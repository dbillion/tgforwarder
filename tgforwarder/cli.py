"""Click CLI for tgforwarder — installable via `uv tool install .` (tgf)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the current working directory (project root), not a parent.
load_dotenv(Path(".env"), override=False)

import click
from rich.console import Console

from . import __version__
from .client import make_client, resolve_entity
from .cache import ForwardCache
from .forward import extract_text, original_filename
from .score import score_chats, format_table, DEFAULT_DB
from . import state as fstate
from .report import ForwardLogger

console = Console()


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
@click.option("--delay", default=1.0, help="Seconds between messages (anti-ban)")
@click.option("--resume", is_flag=True, help="Continue from last processed message id")
@click.option("--start", is_flag=True, help="Start from beginning (ignore saved progress)")
def forward(source, dest, dl_path, limit, process_all, order, session, delay, resume, start):
    """Download media from SOURCE, OCR-rename (Rust/kreuzberg), re-upload to DEST.

    Order: by default forwards the OLDEST files first (chronological from the
    channel's start). Use --order newest for most-recent-first. With no args,
    launches an interactive menu (oldest/newest, start/resume, config).
    Source/dest/path can be preset in .env (SOURCE_CHANNELS/DEST_CHANNELS/FORWARD_PATH).
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
        try:
            src = await resolve_entity(client, source)
            tgts = [await resolve_entity(client, d) for d in dest]
            console.print(f"[green]📥 Source:[/green] {src.title}  [green]📤 Dest:[/green] {', '.join(t.title for t in tgts)}")
            console.print(f"[dim]   order: {order} | download dir: {dl_dir} | resume offset: {offset_id} | all: {process_all}[/dim]")

            # Fast dedup: load done msg_ids into a set (O(1) membership, no per-row SQL).
            done: set[int] = set()
            for t in tgts:
                done |= cache.load_done_set(src.id, t.id)

            count = 0
            max_id = offset_id
            # oldest -> reverse (chronological from start); newest -> default order.
            reverse = (order == "oldest")
            async for msg in client.iter_messages(src, limit=None if process_all else limit,
                                                  min_id=offset_id, reverse=reverse):
                if not process_all and count >= limit:
                    break
                if msg.id > max_id:
                    max_id = msg.id
                caption = getattr(msg, "text", None) or getattr(msg, "caption", None)
                if msg.media:
                    dl = await client.download_media(msg, str(dl_dir / original_filename(msg)))
                    if not dl:
                        continue
                    # Batch OCR: collect paths, extract in one Rust-parallel call.
                    text, suggested = extract_text(dl)
                    final = dl
                    if suggested:
                        new = Path(dl).parent / suggested
                        try:
                            Path(dl).rename(new); final = str(new)
                        except Exception:
                            pass
                    for t in tgts:
                        if msg.id in done:
                            continue
                        sent = await client.send_file(t, final, caption=caption, force_document=True)
                        pending.append({"source_id": src.id, "source_msg_id": msg.id,
                                        "target_id": t.id, "target_msg_id": sent.id if sent else None,
                                        "file_name": final})
                        done.add(msg.id)
                        logger.record(Path(final).name)
                    if os.path.exists(final):
                        os.remove(final)
                elif caption:
                    for t in tgts:
                        if msg.id in done:
                            continue
                        sent = await client.send_message(t, caption)
                        pending.append({"source_id": src.id, "source_msg_id": msg.id,
                                        "target_id": t.id, "target_msg_id": sent.id if sent else None})
                        done.add(msg.id)
                        logger.record(f"text:{msg.id}")
                # Flush marks in chunks (one transaction per 50) — keeps memory/time bounded.
                if len(pending) >= 50:
                    cache.mark_many(pending)
                    pending.clear()
                count += 1
                if delay:
                    await asyncio.sleep(delay)
            if pending:
                cache.mark_many(pending)
                pending.clear()
            # Persist resume point + direction
            fstate.set_progress(st, source, max_id, direction=order)
            fstate.save_state(st)
            console.print(f"[bold green]✨ Done. {cache.stats()}[/bold green]")
            logger.render(console)
        finally:
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
def status():
    """Show configured API id presence (never prints secrets)."""
    ok = bool(os.environ.get("TELEGRAM_API_ID")) and bool(os.environ.get("TELEGRAM_API_HASH"))
    console.print(f"api configured: {'yes' if ok else 'NO — set TELEGRAM_API_ID/TELEGRAM_API_HASH'}")


if __name__ == "__main__":
    cli()
