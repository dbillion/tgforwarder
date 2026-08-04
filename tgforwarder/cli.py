"""Click CLI for tgforwarder — installable via `uv tool install .` (tgf)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import click
from rich.console import Console

from . import __version__
from .client import make_client, resolve_entity
from .cache import ForwardCache
from telethon import utils
from .forward import extract_text, original_filename
from .score import score_chats, format_table, DEFAULT_DB
from . import state as fstate
from .report import ForwardLogger

from dataclasses import dataclass
from functools import wraps
from contextlib import contextmanager
from telethon.tl.custom import Message
import time

console = Console()


# --- Python Tricks (from dbillion/dsa-python-colab-notebooks) applied here ---

@dataclass(frozen=True, slots=True)
class WorkItem:
    """One message queued for forwarding: the source Message plus its derived caption."""
    msg: Message
    caption: str | None


def retry(times: int = 3, delay: float = 0.5):
    """Notebook §7 — wrap a (possibly flaky) call so transient API errors self-heal."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc = None
            for _ in range(times):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:  # transient network/MTProto errors
                    last_exc = exc
                    await asyncio.sleep(delay)
            raise last_exc
        return wrapper
    return decorator


@contextmanager
def timer(label: str):
    """Notebook §3 — print elapsed wall-time for a block, no manual start/stop."""
    start = time.perf_counter()
    try:
        yield
    finally:
        console.print(f"[dim]⏱️  {label}: {time.perf_counter() - start:.2f}s[/dim]")


async def iter_undone(client, src, done_sets: list[set[int]], *, order: str, min_id: int = 0):
    """Lazy pipeline (notebook §9): yield only source messages not yet delivered to ALL targets.

    `done_sets` is one set per target (dedup is per-target, so each target ends up a
    complete copy). A message is skipped only if it is already present in EVERY target;
    if even one target lacks it, it is forwarded (and the batch delivers it to all targets,
    but only the missing ones keep it after verification).
    """
    reverse = order == "oldest"
    async for msg in client.iter_messages(src, min_id=min_id, reverse=reverse):
        if all(msg.id in s for s in done_sets):
            continue
        caption = (getattr(msg, "text", None) or getattr(msg, "message", None)
                   or getattr(msg, "caption", None))
        yield WorkItem(msg=msg, caption=caption)


async def iter_source_ids_recency(client, target, src_id: int, cold_after: int = 200) -> set[int]:
    """Collect saved_from_msg_id for our source, scanning NEWEST-first and stopping on a
    cold streak.

    Telethon/MTProto has NO server-side filter for `saved_from_peer` (probed: from_user
    returns 0 because a forwarded copy's sender is you/bot, not the source). So we must
    fetch message objects — but we can bound the cost: forwards from a source cluster near
    when they arrived, so once we hit `cold_after` consecutive non-matches from the top we
    stop. This turns a 14k download into a few-hundred-message window on steady state.
    """
    delivered: set[int] = set()
    cold = 0
    async for m in client.iter_messages(target, reverse=False):  # newest first
        fwd = getattr(m, "fwd_from", None)
        if _is_from_source(fwd, src_id):
            sf = getattr(fwd, "saved_from_msg_id", None)
            if sf:
                delivered.add(sf)
                cold = 0
                continue
        cold += 1
        if cold >= cold_after:
            break
    return delivered


async def iter_source_ids_full(client, target, src_id: int) -> set[int]:
    """Authoritative: collect ALL saved_from_msg_id for our source (full target scan)."""
    delivered: set[int] = set()
    async for m in client.iter_messages(target):
        fwd = getattr(m, "fwd_from", None)
        if _is_from_source(fwd, src_id):
            sf = getattr(fwd, "saved_from_msg_id", None)
            if sf:
                delivered.add(sf)
    return delivered


import hashlib


def content_hash_of(msg) -> str:
    """Stable content key for COPY mode (no saved_from_peer available).

    Uses text + media descriptor (name+size+type). Media bytes are NOT hashed here
    (that would require downloading); name+size is a stable, collision-resistant key
    for re-post dedup, and we only download when we actually re-post.
    """
    text = (getattr(msg, "message", None) or getattr(msg, "text", None)
            or getattr(msg, "caption", None) or "")
    media = msg.media
    if media is not None:
        name = original_filename(msg)
        size = getattr(getattr(media, "document", None), "size", None) or getattr(media, "size", None) or 0
        kind = type(media).__name__
        text += f"||{kind}|{name}|{size}"
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def _is_from_source(fwd, src_id: int) -> bool:
    """True if a forwarded message's saved_from_peer matches the source peer.

    The source may be a channel, user, or chat, so compare against the
    correctly-typed peer (PeerChannel / PeerUser / PeerChat) rather than a
    hardcoded PeerUser. Otherwise channel-sourced forwards never match and the
    dedup rebuild / final verification / dedupe command silently skip everything.
    """
    return bool(fwd and getattr(fwd, "saved_from_peer", None) == utils.get_peer(src_id))


async def verify_ids_exist(client, target, ids: list[int]) -> set[int]:
    """Re-read message ids from the target to confirm they actually landed.

    Used for verified write-through: forward_messages can return a truthy result for a
    message that never persisted (deleted-account peers). We must not mark such ids 'done'
    or we'd skip them forever AND we'd report false success. Returns the subset of ids
    that genuinely exist in the target.
    """
    ids = [i for i in ids if i]
    if not ids:
        return set()
    try:
        got = await client.get_messages(target, ids=ids)
    except Exception:
        return set()  # unknown -> treat as not-verified (will retry next run)
    return {g.id for g in got if g is not None and getattr(g, "id", None)}


@retry(times=3, delay=0.5)
async def _forward_messages(client, target, msgs):
    """Notebook §7: transient MTProto/network errors retry automatically."""
    return await client.forward_messages(target, msgs)


async def _forward_batch(client, tgts, batch, done, cache, pending, logger, verify_fn,
                         mark_threshold: int = 50, src_id: int | None = None) -> int:
    """Forward one batch to all targets, VERIFY it landed, and only persist verified rows.

    - `done` already contains the batch's source ids (optimistic reservation by caller).
    - If a message fails to forward OR fails verification, its id is removed from `done`
      so a later run will retry it (never silently dropped, never double-counted).
    Returns the number of messages VERIFIED as delivered to the primary target.
    """
    verified_count = 0
    batch_msgs = [item.msg for item in batch]
    primary = tgts[0]
    for t in tgts:
        try:
            res = await _forward_messages(client, t, batch_msgs)
        except Exception:
            res = None
        if not res:
            # Hard failure: release reservation in ALL targets so this batch retries next run.
            for m in batch_msgs:
                for s in done.values():
                    s.discard(m.id)
            continue
        sent_list = res if isinstance(res, list) else [res]
        returned_ids = [getattr(s, "id", None) for s in sent_list]
        # Verify against the primary target (consistent enough for single/multi-target).
        verified_ids = await verify_fn(client, primary, returned_ids)
        for item, sent in zip(batch, sent_list):
            m = item.msg
            sid = getattr(sent, "id", None)
            if sid in verified_ids:
                pending.append({"source_id": src_id if src_id is not None else getattr(m, "chat_id", None),
                                "source_msg_id": m.id,
                                "target_id": t.id, "target_msg_id": sid,
                                "file_name": (getattr(sent, "file", None) and getattr(sent.file, "name", None))
                                             or (m.media and original_filename(m)) or f"msg:{m.id}"})
                logger.record(pending[-1]["file_name"])
                verified_count += 1
            else:
                # Ghost forward (returned but not in target): release reservation in ALL
                # targets -> retry next run.
                for s in done.values():
                    s.discard(m.id)
        # Flush marks in chunks (bounded memory, bounded commits).
        if len(pending) >= mark_threshold:
            cache.mark_many(pending)
            pending.clear()
    return verified_count


@click.group()
@click.version_option(__version__, prog_name="tgf")
def cli():
    """tgforwarder — Telegram MTProto media-forwarder with OCR + usefulness scoring."""


@cli.command()
@click.option("--source", "source", default=None, help="Source channel (or SOURCE_CHANNELS in .env)")
@click.option("--dest", "dest", default=(), multiple=True, help="Destination channel(s) — repeatable (or DEST_CHANNELS in .env)")
@click.option("--path", "dl_path", default=None, help="Local download dir for media (default: ./downloads)")
@click.option("--limit", default=50, help="Max messages to process (ignored with --all)")
@click.option("--all", "process_all", is_flag=True, help="Process every message in the channel")
@click.option("--order", "order", type=click.Choice(["oldest", "newest"]), default="oldest",
              help="Forward order. Default: oldest (chronological from channel start)")
@click.option("--session", default=None, help="Session name (default: forwarder_session1)")
@click.option("--delay", default=1.0, help="Seconds between BATCHES (anti-ban); one API call per batch")
@click.option("--batch", "batch_size", default=25, help="Messages per forward API call (one call moves the whole batch)")
@click.option("--resume", is_flag=True, help="Continue from last processed message id")
@click.option("--start", is_flag=True, help="Start from beginning (ignore saved progress)")
@click.option("--rebuild-cache/--no-rebuild-cache", "rebuild_cache", default=True,
              help="Before forwarding, ensure the dedup cache reflects what is ACTUALLY in the "
                   "target (ground truth via saved_from_msg_id). When the persisted cache "
                   "already has entries it is trusted (O(1) load); only an empty/stale cache "
                   "triggers a full target scan. Use --force-rebuild to always re-scan.")
@click.option("--force-rebuild", "force_rebuild", is_flag=True, default=False,
              help="Always perform the full target scan to rebuild ground truth, even if the "
                   "cache is already populated. Slower, but authoritative.")
@click.option("--quick-rebuild", "quick_rebuild", is_flag=True, default=False,
              help="Recency-bounded rebuild: scan the target newest-first and stop after a cold "
                   "streak. Fast (no full download) but may miss old forwards. Use only on a "
                   "trusted, already-populated cache.")
@click.option("--copy", "copy_mode", is_flag=True, default=False,
              help="COPY mode for protected chats that block forwarding (ChatForwardsRestricted). "
                   "Downloads each message and re-posts it to the targets (no saved_from linkage). "
                   "Dedup is by content hash instead of saved_from_msg_id.")
def forward(source, dest, dl_path, limit, process_all, order, session, delay, batch_size, resume, start, rebuild_cache, force_rebuild, quick_rebuild, copy_mode):
    """Forward messages/media from SOURCE to DEST (native copy; Rust/kreuzberg OCR fallback).

    Order: by default forwards the OLDEST files first (chronological from the
    channel's start). Use --order newest for most-recent-first. With no args,
    launches an interactive menu (oldest/newest, start/resume, config).
    Source/dest/path can be preset in .env (SOURCE_CHANNELS/DEST_CHANNELS/FORWARD_PATH).
    Batches many messages into one API call (--batch) to maximize throughput while
    staying under the rate limit (--delay applies per batch).
    """
    # Resolve from env first; go interactive only if BOTH still unset.
    if not source:
        source = os.environ.get("SOURCE_CHANNELS", "").strip()
    if not dest:
        raw = os.environ.get("DEST_CHANNELS", os.environ.get("TARGET_CHANNELS", ""))
        dest = tuple(d.strip() for d in raw.split(",") if d.strip())

    if not source and not dest:
        source, dest, resume, order = _interactive_menu()
    if not source or not dest:
        raise SystemExit("Provide --source/--dest or set SOURCE_CHANNELS/DEST_CHANNELS in .env")
    if start and resume:
        raise SystemExit("Use only one of --start / --resume")

    dl_dir = Path(dl_path) if dl_path else Path(os.environ.get("FORWARD_PATH", "downloads"))
    dl_dir.mkdir(parents=True, exist_ok=True)

    st = fstate.load_state()
    offset_id = 0
    if resume and not start:
        offset_id = fstate.last_id_for(st, source)
        order = fstate.direction_for(st, source) or order
        console.print(f"[cyan]↩️  Resuming from message id {offset_id} ({order})[/cyan]")

    logger = ForwardLogger()
    # Batch-write buffer for O(1)-chunked DB commits (scales to 5000+).
    pending: list[dict] = []

    async def run():
        client = make_client(session)
        cache = ForwardCache()
        await client.start()
        start = time.perf_counter()
        try:
            src = await resolve_entity(client, source)
            tgts = [await resolve_entity(client, d) for d in dest]
            # Instant total message count for the source (no iteration needed).
            try:
                _zero = await client.get_messages(src, limit=0)
                src_total = getattr(_zero, "total", None) or getattr(src, "message_count", None)
            except Exception:
                src_total = getattr(src, "message_count", None)
            console.print(f"[dim]   source total messages (instant): {src_total}[/dim]")
            src_label = getattr(src, "title", None) or getattr(src, "first_name", None) or f"peer:{getattr(src, 'user_id', getattr(src, 'channel_id', source))}"
            console.print(f"[green]📥 Source:[/green] {src_label}  [green]📤 Dest:[/green] {', '.join(getattr(t, 'title', None) or getattr(t, 'first_name', None) or str(t) for t in tgts)}")
            console.print(f"[dim]   order: {order} | download dir: {dl_dir} | resume offset: {offset_id} | all: {process_all}[/dim]")

            # Fast dedup: load done msg_ids into a set (O(1) membership, no per-row SQL).
            # SAFETY: the cache can be inflated if forward_messages returned truthy but the
            # message never actually persisted (common with deleted-account peers). That
            # makes us skip real messages forever. So when --rebuild-cache (default), we
            # first derive GROUND TRUTH from the target: scan Saved/DMs for messages whose
            # fwd_from.saved_from_peer == source, collect their saved_from_msg_id, and
            # overwrite the cache with exactly that set. Only then do we dedup against truth.
            done_by_target: dict[int, set] = {}
            for t in tgts:
                if copy_mode:
                    # COPY mode: dedup by content hash. No saved_from rebuild (none exists).
                    existing_h = cache.load_done_hashes(src.id, t.id)
                    console.print(f"[dim]   COPY mode: loaded {len(existing_h)} content hashes for target '{getattr(t, 'title', None) or getattr(t, 'first_name', None) or t.id}'[/dim]")
                    done_by_target[t.id] = existing_h
                    continue
                # Load persisted ground truth FIRST (O(1) indexed query, no network scan).
                existing = cache.load_done_set(src.id, t.id)
                need_scan = force_rebuild or (rebuild_cache and not existing)
                if need_scan:
                    if quick_rebuild and not force_rebuild:
                        console.print(f"[yellow]🔍 Quick rebuild (recency-bounded) from target '{getattr(t, 'title', None) or getattr(t, 'first_name', None) or t.id}'...[/yellow]")
                        delivered = await iter_source_ids_recency(client, t, src.id)
                    else:
                        console.print(f"[yellow]🔍 Full rebuild from target '{getattr(t, 'title', None) or getattr(t, 'first_name', None) or t.id}' (ground truth)...[/yellow]")
                        delivered = await iter_source_ids_full(client, t, src.id)
                    removed = cache.rebuild_done_set(src.id, t.id, delivered)
                    console.print(f"[dim]   cache had {removed} rows; rebuilt with {len(delivered)} verified-delivered ids[/dim]")
                    done_by_target[t.id] = delivered
                else:
                    console.print(f"[dim]   using persisted cache ({len(existing)} delivered ids) — skip target scan[/dim]")
                    done_by_target[t.id] = existing

            count = 0
            run_forwarded = 0
            max_id = offset_id
            # When rebuilding the cache from ground truth, do NOT skip by message id:
            # the truthful `done` set handles dedup, and a min_id filter would wrongly
            # exclude messages with lower ids that were never actually delivered.
            min_id_arg = 0 if rebuild_cache else offset_id
            if not copy_mode:
                batch: list[WorkItem] = []
                # Producer: lazy generator yields only undelivered messages (per-target dedup).
                producer = iter_undone(client, src, list(done_by_target.values()), order=order, min_id=min_id_arg)
                async for item in producer:
                    if not process_all and count >= limit:
                        break
                    if item.msg.id > max_id:
                        max_id = item.msg.id
                    # OPTIMISTIC RESERVATION: reserve this id in EVERY target's set so a
                    # restart/re-run can never re-pick it. PERSIST only after verify confirms.
                    for s in done_by_target.values():
                        s.add(item.msg.id)
                    batch.append(item)
                    count += 1
                    # When the batch is full, forward all messages in ONE API call per target.
                    if len(batch) >= batch_size:
                        run_forwarded += await _forward_batch(client, tgts, batch, done_by_target, cache, pending,
                                                              logger, verify_ids_exist, mark_threshold=50,
                                                              src_id=src.id)
                        batch.clear()
                        if delay:
                            await asyncio.sleep(delay)
                # Forward any remaining partial batch.
                if batch:
                    run_forwarded += await _forward_batch(client, tgts, batch, done_by_target, cache, pending,
                                                          logger, verify_ids_exist, mark_threshold=50,
                                                          src_id=src.id)
                    batch.clear()
            if pending:
                cache.mark_many(pending)
                pending.clear()
            # ---- COPY MODE (protected chats, BATCHED download→upload→delete) ----
            # No forward API works -> download each msg + re-post via send_message. To keep disk
            # flat at 65k+ scale, we process in batches: download a batch's media, upload to all
            # targets that lack each hash, then PERMANENTLY delete the batch's temp files before
            # fetching the next batch. Dedup by content hash (no saved_from_peer in copy mode).
            if copy_mode:
                import tempfile, os as _os
                copy_pending: list[dict] = []
                copy_batch: list = []          # collected (msg, hash, text, media_path) tuples
                temp_paths: list[str] = []    # files to delete after this batch uploads
                copy_reverse = (order == "oldest")
                copy_min_id = 0 if rebuild_cache else offset_id

                async def _flush_copy_batch():
                    """Upload the current copy batch to all targets, mark, then delete temp files."""
                    nonlocal run_forwarded
                    for msg, h, text, media_path in copy_batch:
                        for t in tgts:
                            if h in done_by_target.get(t.id, set()):
                                continue  # already delivered to this target
                            try:
                                sent = (await client.send_message(t, message=text, file=media_path)
                                        if media_path else await client.send_message(t, message=text))
                                if sent:
                                    done_by_target[t.id].add(h)
                                    run_forwarded += 1
                                    copy_pending.append({"source_id": src.id, "source_msg_id": msg.id,
                                                         "target_id": t.id, "target_msg_id": sent.id,
                                                         "file_name": original_filename(msg),
                                                         "content_hash": h})
                                    logger.record(original_filename(msg))
                            except Exception as e:
                                console.print(f"[yellow]⚠️  send to target {t.id} failed (msg {msg.id}): {e}[/yellow]")
                                # do NOT add to done_by_target -> retries next run
                            if delay:
                                await asyncio.sleep(delay)
                    # Persist cache marks for this batch.
                    if copy_pending:
                        cache.mark_many(copy_pending)
                        copy_pending.clear()
                    # PERMANENTLY delete every downloaded temp file for this batch.
                    for p in temp_paths:
                        try:
                            _os.unlink(p)
                        except OSError:
                            pass
                    temp_paths.clear()
                    copy_batch.clear()

                async for msg in client.iter_messages(src, min_id=copy_min_id, reverse=copy_reverse):
                    if not process_all and count >= limit:
                        break
                    if msg.id > max_id:
                        max_id = msg.id
                    h = content_hash_of(msg)
                    if all(h in s for s in done_by_target.values()):
                        continue
                    text = (getattr(msg, "message", None) or getattr(msg, "text", None)
                            or getattr(msg, "caption", None) or "")
                    media_path = None
                    if msg.media:
                        try:
                            tf = tempfile.NamedTemporaryFile(delete=False, suffix=Path(original_filename(msg)).suffix)
                            tf.close()
                            media_path = await client.download_media(msg, file=tf.name)
                            if media_path:
                                temp_paths.append(str(media_path))
                        except Exception as e:
                            console.print(f"[yellow]⚠️  download failed for msg {msg.id}: {e}[/yellow]")
                            continue
                    copy_batch.append((msg, h, text, media_path))
                    count += 1
                    # Download→upload→delete once the batch is full (flat disk footprint).
                    if len(copy_batch) >= batch_size:
                        await _flush_copy_batch()
                # Flush any remaining partial batch.
                if copy_batch:
                    await _flush_copy_batch()
            # Persist resume point + direction
            fstate.set_progress(st, source, max_id, direction=order)
            fstate.save_state(st)
            # FINAL VERIFICATION (ground truth, FORWARD mode): re-scan EVERY target and count
            # messages that originated from the source (saved_from_peer == src). In COPY mode
            # the target messages have no saved_from_peer (we re-posted them), so this scan
            # would always return 0; instead, the cache hash-union is the honest delivered count.
            final_delivered = 0
            known_total = len(set().union(*done_by_target.values())) if done_by_target else 0
            if not copy_mode:
                try:
                    for t in tgts:
                        async for m in client.iter_messages(t):
                            fwd = getattr(m, "fwd_from", None)
                            if _is_from_source(fwd, src.id):
                                final_delivered += 1
                except Exception:
                    final_delivered = -1
            else:
                # Copy mode: report the cache hash union as the verified delivered count.
                final_delivered = known_total
            pct = (f"{final_delivered / src_total * 100:.1f}%" if (final_delivered >= 0 and src_total) else "n/a")
            console.print(
                f"[bold green]✨ Done.[/bold green] "
                f"forwarded this run: [cyan]{run_forwarded}[/cyan] | "
                f"known delivered (cache, union): [yellow]{known_total}[/yellow] | "
                f"VERIFIED: [green]{final_delivered if final_delivered >= 0 else 'n/a'}[/green] / "
                f"source total: {src_total} ({pct})"
                + (" [dim](COPY mode)[/dim]" if copy_mode else "")
            )
            logger.render(console)
        finally:
            elapsed = time.perf_counter() - start
            console.print(f"[dim]⏱️  forward run: {elapsed:.2f}s[/dim]")
            await client.disconnect()
            cache.close()

    asyncio.run(run())


def _interactive_menu() -> tuple[str, tuple[str, ...], bool, str]:
    """Prompt user for source/dest/path + order + mode (mirrors original telbot menu)."""
    console.print("\n[bold cyan]🤖 tgforwarder — interactive[/bold cyan]")
    src = click.prompt("📥 Source channel (name/@handle/ID)", default=os.environ.get("SOURCE_CHANNELS", ""))
    dst = click.prompt("📤 Destination channel(s), comma-separated", default=os.environ.get("DEST_CHANNELS", ""))
    click.prompt("📂 Download path", default=os.environ.get("FORWARD_PATH", "downloads"), show_default=True)
    order = click.prompt("▶️  Forward order", type=click.Choice(["oldest", "newest"]), default="oldest")
    mode = click.prompt("🔁 Mode", type=click.Choice(["start", "resume"]), default="start")
    return (src.strip(), tuple(d.strip() for d in dst.split(",") if d.strip()),
            (mode == "resume"), order)


@cli.command()
@click.option("--db", default=str(DEFAULT_DB), help="Path to tg-cli messages.db")
@click.option("--topic", default=None, help="Comma/space-separated research topics to boost relevance")
@click.option("--min-score", default=0.0, type=float)
@click.option("--top", default=20, type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of table")
def score(db, topic, min_score, top, as_json):
    """Rank chats by usefulness (research/scraping triage)."""
    if not Path(db).exists():
        raise SystemExit(f"DB not found at {db}. Run: tg-user sync-all -n 200")
    scored = score_chats(db, topics=topic, min_score=min_score, top=top)
    if as_json:
        import json as _json
        console.print(_json.dumps(scored, indent=2, default=str))
    else:
        console.print(format_table(scored))


@cli.command()
@click.option("--source", "source", required=True, help="Channel to test OCR against")
@click.option("--session", default=None)
def test_ocr(source, session):
    """Test OCR on the last 3 media messages in SOURCE."""

    async def run():
        client = make_client(session)
        await client.start()
        try:
            src = await resolve_entity(client, source)
            n = 0
            async for msg in client.iter_messages(src, limit=10):
                if msg.media and n < 3:
                    dl = await client.download_media(msg, str(Path.cwd() / original_filename(msg)))
                    if dl:
                        text, suggested = extract_text(dl)
                        console.print(f"[cyan]msg {msg.id}:[/cyan] {text[:200] if text else 'no text'} -> {suggested}")
                        if os.path.exists(dl):
                            os.remove(dl)
                    n += 1
        finally:
            await client.disconnect()

    asyncio.run(run())


@cli.command()
@click.option("--session", default=None, help="Session name (defaults to TG_SESSION_NAME / forwarder_session1)")
@click.option("--phone", default=None, help="Phone number (e.g. +1234567890). If omitted, you are prompted interactively.")
@click.option("--password", default=None, help="2FA cloud password, if your account has one. Prompted if omitted.")
def login(session, phone, password):
    """Authenticate the Telegram session (one-time).

    Telegram sends a login code to your phone/other client; enter it when
    prompted. If your account has 2FA enabled you'll also be asked for the
    cloud password. The authorized session is saved to the data dir so every
    other tgf command reuses it — you only do this once.

    Runs interactively by default. For headless use, pass --phone/--password.
    """
    async def run():
        # Ensure creds are present (prompts once if missing).
        client = make_client(session)
        # Read phone/password ourselves so login works even when stdin is not a TTY
        # (e.g. piped). Telethon's built-in prompt requires a real terminal.
        effective_phone = phone
        effective_password = password
        if not effective_phone:
            effective_phone = click.prompt("📱 Phone number (e.g. +1234567890)", default="", type=str).strip()
        if not effective_phone:
            console.print("[red]A phone number is required to log in.[/red]")
            raise SystemExit(1)
        try:
            # Only prompt for the 2FA password if the account uses one (Telegram will
            # signal it by raising a password-related error on first attempt).
            try:
                await client.start(phone=effective_phone, password=effective_password or None)
            except Exception as e:
                msg = str(e).lower()
                if "password" in msg or "2fa" in msg or "two-step" in msg:
                    pw = click.prompt("🔒 Cloud password (2FA)", default="", type=str, hide_input=True).strip()
                    if pw:
                        await client.start(phone=effective_phone, password=pw)
                    else:
                        raise
                elif "api_id_invalid" in msg or "api_id" in msg and "invalid" in msg:
                    # The credentials exist but Telegram rejects them — almost always
                    # expired/revoked/wrong values from my.telegram.org.
                    console.print(
                        "[red]❌ Telegram rejected your TELEGRAM_API_ID / TELEGRAM_API_HASH.[/red]\n"
                        "   They are present but INVALID. Generate fresh ones at:\n"
                        "   https://my.telegram.org → API development tools → create app\n"
                        "   then update them in your .env file."
                    )
                    raise SystemExit(1)
                else:
                    raise
            me = await client.get_me()
            console.print(
                f"[bold green]✅ Logged in as[/bold green] "
                f"{getattr(me, 'username', None) or getattr(me, 'first_name', None)} "
                f"(id={me.id})"
            )
            console.print(f"[dim]session saved at: {client.session.filename}[/dim]")
        except Exception as e:
            console.print(f"[red]❌ Login failed:[/red] {type(e).__name__}: {e}")
            raise SystemExit(1)
        finally:
            await client.disconnect()

    asyncio.run(run())


@cli.command()
def status():
    """Show configured API id presence (never prints secrets)."""
    ok = bool(os.environ.get("TELEGRAM_API_ID")) and bool(os.environ.get("TELEGRAM_API_HASH"))
    if ok:
        console.print("api configured: yes")
    else:
        console.print("api configured: no — the next tgf command will prompt for "
                      "TELEGRAM_API_ID / TELEGRAM_API_HASH (saved to .env)")


@cli.command()
@click.option("--source", "source", required=True, help="Source channel whose forwards we de-duplicate in the target")
@click.option("--target", "target", required=True, help="Target chat (e.g. Saved Messages id) to clean")
@click.option("--session", default=None, help="Session name (defaults to TG_SESSION_NAME)")
@click.option("--dry-run", is_flag=True, default=False, help="Count duplicates but do NOT delete")
def dedupe(source, target, session, dry_run):
    """Remove duplicate forwarded copies from TARGET, keeping one per source message id.

    The earlier broken runs left multiple copies of the same source message in the target
    (Telegram does not de-dupe native forwards). This scans the target, and for each message
    whose fwd_from.saved_from_peer == SOURCE and saved_from_msg_id was already seen, deletes the
    redundant copy via client.delete_messages. Use --dry-run first to preview.
    """
    async def run():
        client = make_client(session)
        await client.start()
        try:
            src = await resolve_entity(client, source)
            tgt = await resolve_entity(client, target)
            seen: set[int] = set()
            dup_ids: list[int] = []
            console.print(f"[yellow]🔍 Scanning target '{getattr(tgt, 'title', None) or getattr(tgt, 'first_name', None) or tgt.id}' for duplicate forwards from '{getattr(src, 'title', None) or getattr(src, 'first_name', None) or src.id}'...[/yellow]")
            async for m in client.iter_messages(tgt):
                fwd = getattr(m, "fwd_from", None)
                if _is_from_source(fwd, src.id):
                    sf = getattr(fwd, "saved_from_msg_id", None)
                    if sf is not None:
                        if sf in seen:
                            dup_ids.append(m.id)
                        else:
                            seen.add(sf)
            console.print(f"[cyan]unique source messages: {len(seen)} | duplicate copies found: {len(dup_ids)}[/cyan]")
            if not dup_ids:
                console.print("[green]✅ No duplicates to remove.[/green]")
                return
            if dry_run:
                console.print(f"[yellow]--dry-run: would delete {len(dup_ids)} duplicates (no action taken)[/yellow]")
                return
            # Delete in chunks (delete_messages accepts a list).
            chunk = 100
            deleted = 0
            for i in range(0, len(dup_ids), chunk):
                await client.delete_messages(tgt, dup_ids[i:i + chunk])
                deleted += len(dup_ids[i:i + chunk])
                console.print(f"[dim]deleted {deleted}/{len(dup_ids)}[/dim]")
            console.print(f"[green]✅ Removed {deleted} duplicate copies; kept {len(seen)} unique.[/green]")
        finally:
            await client.disconnect()
    asyncio.run(run())


if __name__ == "__main__":
    cli()
