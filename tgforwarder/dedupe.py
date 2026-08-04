"""Dedupe command body: remove duplicate forwarded copies from a target chat."""
from __future__ import annotations

from rich.console import Console

from .client import make_client, resolve_entity
from .peer import _is_from_source

console = Console()


async def dedupe_run(source, target, session, dry_run):
    """Remove duplicate forwarded copies from TARGET, keeping one per source message id.

    The earlier broken runs left multiple copies of the same source message in the target
    (Telegram does not de-dupe native forwards). This scans the target, and for each message
    whose fwd_from.saved_from_peer == SOURCE and saved_from_msg_id was already seen, deletes the
    redundant copy via client.delete_messages. Use --dry-run first to preview.
    """
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
