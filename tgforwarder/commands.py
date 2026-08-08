"""Async command bodies for tgforwarder CLI.

Orchestration layer: each function is the *implementation* of a CLI command.
Auth/dedupe/copy-mode logic lives in their own modules (login.py, dedupe.py,
copy_mode.py) so this file stays readable. The cli.py layer only declares Click
options and forwards parsed args here.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import click
from rich.console import Console

from . import state as fstate
from .cache import ForwardCache
from .client import connect_authorized, make_client, resolve_entity
from .copy_mode import run_copy_mode
from .forward import extract_text, original_filename
from .peer import (
    WorkItem, _forward_batch, _is_from_source, iter_source_ids_full,
    iter_source_ids_recency, iter_undone, verify_ids_exist,
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
    await connect_authorized(client)
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


async def forward_run(source, dest, dl_path, limit, process_all, order, session, delay,
                      batch_size, resume, start, rebuild_cache, force_rebuild,
                      quick_rebuild, copy_mode):
    """Forward SOURCE → DEST (native copy, or COPY mode for protected chats)."""
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
    await connect_authorized(client)
    start_ts = time.perf_counter()
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

        # Ground-truth dedup cache: either trust persisted rows, or rebuild from the
        # target (full scan, or recency-bounded quick scan). In COPY mode we dedup by
        # content hash instead, since re-posted messages carry no saved_from linkage.
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
            # Native forward: lazy producer yields only undelivered messages; each batch
            # is forwarded in ONE API call per target and VERIFIED before being persisted.
            batch: list[WorkItem] = []
            producer = iter_undone(client, src, list(done_by_target.values()), order=order, min_id=min_id_arg)
            async for item in producer:
                if not process_all and count >= limit:
                    break
                if item.msg.id > max_id:
                    max_id = item.msg.id
                for s in done_by_target.values():
                    s.add(item.msg.id)  # optimistic reservation; released on verify failure
                batch.append(item)
                count += 1
                if len(batch) >= batch_size:
                    run_forwarded += await _forward_batch(client, tgts, batch, done_by_target, cache, pending,
                                                          logger, verify_ids_exist, mark_threshold=50,
                                                          src_id=src.id)
                    batch.clear()
                    if delay:
                        await asyncio.sleep(delay)
            if batch:
                run_forwarded += await _forward_batch(client, tgts, batch, done_by_target, cache, pending,
                                                      logger, verify_ids_exist, mark_threshold=50,
                                                      src_id=src.id)
                batch.clear()
        else:
            # COPY mode: download→re-post (protected chats that block native forward).
            count, max_id, run_forwarded = await run_copy_mode(
                client, src, tgts, order=order, rebuild_cache=rebuild_cache, offset_id=offset_id,
                batch_size=batch_size, delay=delay, limit=limit, process_all=process_all,
                done_by_target=done_by_target, cache=cache, logger=logger,
                count=count, max_id=max_id, run_forwarded=run_forwarded,
            )

        if pending:
            cache.mark_many(pending)
            pending.clear()

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
        elapsed = time.perf_counter() - start_ts
        console.print(f"[dim]⏱️  forward run: {elapsed:.2f}s[/dim]")
        await client.disconnect()
        cache.close()
