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
@click.option("--limit", default=50, help="Max messages to process")
@click.option("--session", default=None, help="Session name (default: forwarder_session1)")
@click.option("--delay", default=1.0, help="Seconds between messages (anti-ban)")
@click.option("--resume", is_flag=True, help="Continue from last forwarded message id")
@click.option("--start", is_flag=True, help="Start from beginning (ignore saved progress)")
def forward(source, dest, dl_path, limit, session, delay, resume, start):
    """Download media from SOURCE, OCR-rename, re-upload to DEST channel(s).

    With no args, launches an interactive menu (start / resume / config).
    Source/dest/path can be preset in .env (SOURCE_CHANNELS/DEST_CHANNELS/FORWARD_PATH).
    """
    # Resolve from env first; go interactive only if BOTH still unset.
    if not source:
        source = os.environ.get("SOURCE_CHANNELS", "").strip()
    if not dest:
        raw = os.environ.get("DEST_CHANNELS", os.environ.get("TARGET_CHANNELS", ""))
        dest = tuple(d.strip() for d in raw.split(",") if d.strip())

    if not source and not dest:
        # Interactive menu
        source, dest, resume = _interactive_menu()
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
        console.print(f"[cyan]↩️  Resuming from message id {offset_id}[/cyan]")

    logger = ForwardLogger()

    async def run():
        client = make_client(session)
        cache = ForwardCache()
        await client.start()
        try:
            src = await resolve_entity(client, source)
            tgts = [await resolve_entity(client, d) for d in dest]
            console.print(f"[green]📥 Source:[/green] {src.title}  [green]📤 Dest:[/green] {', '.join(t.title for t in tgts)}")
            console.print(f"[dim]   download dir: {dl_dir} | resume offset: {offset_id}[/dim]")
            count = 0
            max_id = offset_id
            async for msg in client.iter_messages(src, limit=limit, min_id=offset_id):
                if count >= limit:
                    break
                if msg.id > max_id:
                    max_id = msg.id
                caption = getattr(msg, "text", None) or getattr(msg, "caption", None)
                if msg.media:
                    dl = await client.download_media(msg, str(dl_dir / original_filename(msg)))
                    if not dl:
                        continue
                    text, suggested = extract_text(dl)
                    final = dl
                    if suggested:
                        new = Path(dl).parent / suggested
                        try:
                            Path(dl).rename(new); final = str(new)
                        except Exception:
                            pass
                    for t in tgts:
                        if cache.is_done(src.id, msg.id, t.id):
                            continue
                        sent = await client.send_file(t, final, caption=caption, force_document=True)
                        cache.mark(source_id=src.id, source_msg_id=msg.id, target_id=t.id,
                                   target_msg_id=sent.id if sent else None, file_name=final)
                        logger.record(Path(final).name)
                    if os.path.exists(final):
                        os.remove(final)
                elif caption:
                    for t in tgts:
                        if cache.is_done(src.id, msg.id, t.id):
                            continue
                        sent = await client.send_message(t, caption)
                        cache.mark(source_id=src.id, source_msg_id=msg.id, target_id=t.id,
                                   target_msg_id=sent.id if sent else None)
                        logger.record(f"text:{msg.id}")
                count += 1
                if delay:
                    await asyncio.sleep(delay)
            # Persist resume point
            fstate.set_last_id(st, source, max_id)
            fstate.save_state(st)
            console.print(f"[bold green]✨ Done. {cache.stats()}[/bold green]")
            logger.render(console)
        finally:
            await client.disconnect()
            cache.close()

    asyncio.run(run())


def _interactive_menu() -> tuple[str, tuple[str, ...], bool]:
    """Prompt user for source/dest/path + mode (mirrors original telbot menu)."""
    console.print("\n[bold cyan]🤖 tgforwarder — interactive[/bold cyan]")
    src = click.prompt("📥 Source channel (name/@handle/ID)", default=os.environ.get("SOURCE_CHANNELS", ""))
    dst = click.prompt("📤 Destination channel(s), comma-separated", default=os.environ.get("DEST_CHANNELS", ""))
    click.prompt("📂 Download path", default=os.environ.get("FORWARD_PATH", "downloads"), show_default=True)
    mode = click.prompt("▶️  Mode", type=click.Choice(["start", "resume"]), default="start")
    return src.strip(), tuple(d.strip() for d in dst.split(",") if d.strip()), (mode == "resume")


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
