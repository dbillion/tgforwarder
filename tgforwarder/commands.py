"""Async command bodies for tgforwarder CLI.

Each function here is the *implementation* of a CLI command. The cli.py layer
only declares Click options and forwards parsed args into these, keeping the
command logic in one place and importable for testing without a TTY.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import click
from rich.console import Console

from . import state as fstate
from .cache import ForwardCache
from .client import make_client, resolve_entity
from .forward import extract_text, original_filename
from .peer import (
    WorkItem, _forward_batch, _is_from_source, content_hash_of,
    iter_source_ids_full, iter_source_ids_recency, iter_undone, verify_ids_exist,
)
from .report import ForwardLogger
from .score import DEFAULT_DB, format_table, score_chats

console = Console()


def interactive_menu() -> tuple[str, tuple[str, ...], bool, str]:
    """Prompt user for source/dest/path + order + mode (mirrors original telbot menu)."""
    console.print("\n[bold cyan]🤖 tgforwarder — interactive[/bold cyan]")
    src = click.prompt("📥 Source channel (name/@handle/ID)", default=os.environ.get("SOURCE_CHANNELS", ""))
    dst = click.prompt("📤 Destination channel(s), comma-separated", default=os.environ.get("DEST_CHANNELS", ""))
    click.prompt("📂 Download path", default=os.environ.get("FORWARD_PATH", "downloads"), show_default=True)
    order = click.prompt("▶️  Forward order", type=click.Choice(["oldest", "newest"]), default="oldest")
    mode = click.prompt("🔁 Mode", type=click.Choice(["start", "resume"]), default="start")
    return (src.strip(), tuple(d.strip() for d in dst.split(",") if d.strip()),
            (mode == "resume"), order)


async def login_run(session, phone, password):
    """Authenticate the Telegram session (one-time). See cli.login for the user docs."""
    client = make_client(session)
    effective_phone = phone
    effective_password = password
    if not effective_phone:
        effective_phone = click.prompt("📱 Phone number (e.g. +1234567890)", default="", type=str).strip()
    if not effective_phone:
        console.print("[red]A phone number is required to log in.[/red]")
        raise SystemExit(1)
    try:
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
            elif "api_id_invalid" in msg or ("api_id" in msg and "invalid" in msg):
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


def score_run(db, topic, min_score, top, as_json):
    """Rank chats by usefulness (research/scraping triage)."""
    if not Path(db).exists():
        raise SystemExit(f"DB not found at {db}. Run: tg-user sync-all -n 200")
    scored = score_chats(db, topics=topic, min_score=min_score, top=top)
    if as_json:
        import json as _json
        console.print(_json.dumps(scored, indent=2, default=str))
    else:
        console.print(format_table(scored))


async def test_ocr_run(source, session):
    """Test OCR on the last 3 media messages in SOURCE."""
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


async def dedupe_run(source, target, session, dry_run):
    """Remove duplicate forwarded copies from TARGET, keeping one per source message id."""
    from .peer import _is_from_source

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
        chunk = 100
        deleted = 0
        for i in range(0, len(dup_ids), chunk):
            await client.delete_messages(tgt, dup_ids[i:i + chunk])
            deleted += len(dup_ids[i:i + chunk])
            console.print(f"[dim]deleted {deleted}/{len(dup_ids)}[/dim]")
        console.print(f"[green]✅ Removed {deleted} duplicate copies; kept {len(seen)} unique.[/green]")
    finally:
        await client.disconnect()


async def forward_run(source, dest, dl_path, limit, process_all, order, session, delay,
                      batch_size, resume, start, rebuild_cache, force_rebuild,
                      quick_rebuild, copy_mode):
    """Forward SOURCE → DEST. See cli.forward for user docs."""
    if not source:
        source = os.environ.get("SOURCE_CHANNELS", "").strip()
    if not dest:
        raw = os.environ.get("DEST_CHANNELS", os.environ.get("TARGET_CHANNELS", ""))
        dest = tuple(d.strip() for d in raw.split(",") if d.strip())

    if not source and not dest:
        source, dest, resume, order = interactive_menu()
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
    pending: list[dict] = []

    client = make_client(session)
    cache = ForwardCache()
    await client.start()
    start_ts = __import__("time").perf_counter()
    try:
        src = await resolve_entity(client, source)
        tgts = [await resolve_entity(client, d) for d in dest]
        try:
            _zero = await client.get_messages(src, limit=0)
            src_total = getattr(_zero, "total", None) or getattr(src, "message_count", None)
        except Exception:
            src_total = getattr(src, "message_count", None)
        console.print(f"[dim]   source total messages (instant): {src_total}[/dim]")
        src_label = getattr(src, "title", None) or getattr(src, "first_name", None) or f"peer:{getattr(src, 'user_id', getattr(src, 'channel_id', source))}"
        console.print(f"[green]📥 Source:[/green] {src_label}  [green]📤 Dest:[/green] {', '.join(getattr(t, 'title', None) or getattr(t, 'first_name', None) or str(t) for t in tgts)}")
        console.print(f"[dim]   order: {order} | download dir: {dl_dir} | resume offset: {offset_id} | all: {process_all}[/dim]")

        done_by_target: dict[int, set] = {}
        for t in tgts:
            if copy_mode:
                existing_h = cache.load_done_hashes(src.id, t.id)
                console.print(f"[dim]   COPY mode: loaded {len(existing_h)} content hashes for target '{getattr(t, 'title', None) or getattr(t, 'first_name', None) or t.id}'[/dim]")
                done_by_target[t.id] = existing_h
                continue
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
        min_id_arg = 0 if rebuild_cache else offset_id
        if not copy_mode:
            batch: list[WorkItem] = []
            producer = iter_undone(client, src, list(done_by_target.values()), order=order, min_id=min_id_arg)
            async for item in producer:
                if not process_all and count >= limit:
                    break
                if item.msg.id > max_id:
                    max_id = item.msg.id
                for s in done_by_target.values():
                    s.add(item.msg.id)
                batch.append(item)
                count += 1
                if len(batch) >= batch_size:
                    run_forwarded += await _forward_batch(client, tgts, batch, done_by_target, cache, pending,
                                                          logger, verify_ids_exist, mark_threshold=50,
                                                          src_id=src.id)
                    batch.clear()
                    if delay:
                        await __import__("asyncio").sleep(delay)
            if batch:
                run_forwarded += await _forward_batch(client, tgts, batch, done_by_target, cache, pending,
                                                      logger, verify_ids_exist, mark_threshold=50,
                                                      src_id=src.id)
                batch.clear()
        if pending:
            cache.mark_many(pending)
            pending.clear()

        # ---- COPY MODE (protected chats, BATCHED download→upload→delete) ----
        if copy_mode:
            copy_pending: list[dict] = []
            copy_batch: list = []
            temp_paths: list[str] = []
            copy_reverse = (order == "oldest")
            copy_min_id = 0 if rebuild_cache else offset_id

            async def _flush_copy_batch():
                nonlocal run_forwarded
                for msg, h, text, media_path in copy_batch:
                    for t in tgts:
                        if h in done_by_target.get(t.id, set()):
                            continue
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
                        if delay:
                            await __import__("asyncio").sleep(delay)
                if copy_pending:
                    cache.mark_many(copy_pending)
                    copy_pending.clear()
                for p in temp_paths:
                    try:
                        os.unlink(p)
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
                if len(copy_batch) >= batch_size:
                    await _flush_copy_batch()
            if copy_batch:
                await _flush_copy_batch()

        fstate.set_progress(st, source, max_id, direction=order)
        fstate.save_state(st)

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
        elapsed = __import__("time").perf_counter() - start_ts
        console.print(f"[dim]⏱️  forward run: {elapsed:.2f}s[/dim]")
        await client.disconnect()
        cache.close()
