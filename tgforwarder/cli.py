"""Click CLI for tgforwarder — installable via `uv tool install .` (tgf).

This module is intentionally thin: it declares Click options and wires them to
the async command bodies in `tgforwarder.commands` and the pure primitives in
`tgforwarder.peer`. Command *logic* lives in commands.py so it stays importable
and testable without a TTY.
"""
from __future__ import annotations

import asyncio

import click

from . import __version__
from .commands import forward_run, score_run, test_ocr_run
from .dedupe import dedupe_run
from .login import login_run
from .score import DEFAULT_DB

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
              help="Ensure the dedup cache reflects what is ACTUALLY in the target (ground truth).")
@click.option("--force-rebuild", "force_rebuild", is_flag=True, default=False,
              help="Always perform the full target scan to rebuild ground truth.")
@click.option("--quick-rebuild", "quick_rebuild", is_flag=True, default=False,
              help="Recency-bounded rebuild: fast but may miss old forwards.")
@click.option("--copy", "copy_mode", is_flag=True, default=False,
              help="COPY mode for protected chats (download→re-post; dedup by content hash).")
def forward(source, dest, dl_path, limit, process_all, order, session, delay, batch_size,
            resume, start, rebuild_cache, force_rebuild, quick_rebuild, copy_mode):
    """Forward messages/media from SOURCE to DEST (native copy; Rust/kreuzberg OCR fallback).

    Order: by default forwards the OLDEST files first. Use --order newest for
    most-recent-first. With no args, launches an interactive menu. Source/dest/path
    can be preset in .env (SOURCE_CHANNELS/DEST_CHANNELS/FORWARD_PATH).
    """
    asyncio.run(forward_run(source, dest, dl_path, limit, process_all, order, session, delay,
                            batch_size, resume, start, rebuild_cache, force_rebuild,
                            quick_rebuild, copy_mode))


@cli.command()
@click.option("--session", default=None, help="Session name (defaults to TG_SESSION_NAME / forwarder_session1)")
@click.option("--phone", default=None, help="Phone number (e.g. +1234567890). If omitted, you are prompted interactively.")
@click.option("--password", default=None, help="2FA cloud password, if your account has one. Prompted if omitted.")
def login(session, phone, password):
    """Authenticate the Telegram session (one-time).

    Telegram sends a login code to your phone/other client; enter it when prompted.
    If your account has 2FA enabled you'll also be asked for the cloud password. The
    authorized session is saved to the data dir so every other tgf command reuses it.
    """
    asyncio.run(login_run(session, phone, password))


@cli.command()
def status():
    """Show configured API id presence (never prints secrets)."""
    import os
    ok = bool(os.environ.get("TELEGRAM_API_ID")) and bool(os.environ.get("TELEGRAM_API_HASH"))
    if ok:
        click.echo("api configured: yes")
    else:
        click.echo("api configured: no — the next tgf command will prompt for "
                   "TELEGRAM_API_ID / TELEGRAM_API_HASH (saved to .env)")


@cli.command()
@click.option("--db", default=str(DEFAULT_DB), help="Path to tg-cli messages.db")
@click.option("--topic", default=None, help="Comma/space-separated research topics to boost relevance")
@click.option("--min-score", default=0.0, type=float)
@click.option("--top", default=20, type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of table")
def score(db, topic, min_score, top, as_json):
    """Rank chats by usefulness (research/scraping triage)."""
    score_run(db, topic, min_score, top, as_json)


@cli.command()
@click.option("--source", "source", required=True, help="Channel to test OCR against")
@click.option("--session", default=None)
def test_ocr(source, session):
    """Test OCR on the last 3 media messages in SOURCE."""
    asyncio.run(test_ocr_run(source, session))


@cli.command()
@click.option("--source", "source", required=True, help="Source channel whose forwards we de-duplicate in the target")
@click.option("--target", "target", required=True, help="Target chat (e.g. Saved Messages id) to clean")
@click.option("--session", default=None, help="Session name (defaults to TG_SESSION_NAME)")
@click.option("--dry-run", is_flag=True, default=False, help="Count duplicates but do NOT delete")
def dedupe(source, target, session, dry_run):
    """Remove duplicate forwarded copies from TARGET, keeping one per source message id."""
    asyncio.run(dedupe_run(source, target, session, dry_run))


if __name__ == "__main__":
    cli()
