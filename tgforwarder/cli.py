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
from .forward import extract_text, original_filename
from .score import score_chats, format_table, DEFAULT_DB

console = Console()


@click.group()
@click.version_option(__version__, prog_name="tgf")
def cli():
    """tgforwarder — Telegram MTProto media-forwarder with OCR + usefulness scoring."""


@cli.command()
@click.option("--source", "source", required=True, help="Source channel name/@handle/ID")
@click.option("--target", "target", required=True, multiple=True, help="Target channel (repeatable)")
@click.option("--limit", default=50, help="Max messages to process")
@click.option("--session", default=None, help="Session name (default: forwarder_session1)")
@click.option("--delay", default=1.0, help="Seconds between messages (anti-ban)")
def forward(source, target, limit, session, delay):
    """Download media from SOURCE, OCR-rename, re-upload to TARGET(s)."""

    async def run():
        client = make_client(session)
        cache = ForwardCache()
        await client.start()
        try:
            src = await resolve_entity(client, source)
            tgts = [await resolve_entity(client, t) for t in target]
            console.print(f"[green]Source:[/green] {src.title}  [green]Targets:[/green] {', '.join(t.title for t in tgts)}")
            count = 0
            async for msg in client.iter_messages(src, limit=limit):
                if count >= limit:
                    break
                caption = getattr(msg, "text", None) or getattr(msg, "caption", None)
                if msg.media:
                    dl = await client.download_media(msg, str(Path.cwd() / original_filename(msg)))
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
                    if os.path.exists(final):
                        os.remove(final)
                elif caption:
                    for t in tgts:
                        if cache.is_done(src.id, msg.id, t.id):
                            continue
                        sent = await client.send_message(t, caption)
                        cache.mark(source_id=src.id, source_msg_id=msg.id, target_id=t.id,
                                   target_msg_id=sent.id if sent else None)
                count += 1
                if delay:
                    await asyncio.sleep(delay)
            console.print(f"[bold green]Done. {cache.stats()}[/bold green]")
        finally:
            await client.disconnect()
            cache.close()

    asyncio.run(run())


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
