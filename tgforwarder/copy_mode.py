"""COPY-mode pipeline for tgforwarder (protected chats that block native forwards).

Separated from forward_run so that module stays focused on the native-forward
orchestration. Copy mode downloads each message and re-posts it (no saved_from
linkage), deduping by content hash. Files are flushed (deleted) per batch to keep
the disk footprint flat at 65k+ scale.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from rich.console import Console

from .cache import ForwardCache
from .forward import original_filename
from .peer import content_hash_of

console = Console()


async def run_copy_mode(
    client, src, tgts, *, order: str, rebuild_cache: bool, offset_id: int,
    batch_size: int, delay: float, limit: int, process_all: bool,
    done_by_target: dict[int, set], cache: ForwardCache, logger,
    count: int, max_id: int, run_forwarded: int,
) -> tuple[int, int, int]:
    """Download→upload→delete each source message to every target that lacks its hash.

    Returns the updated (count, max_id, run_forwarded) so the caller can report totals
    and persist resume state.
    """
    copy_pending: list[dict] = []
    copy_batch: list = []          # (msg, hash, text, media_path) tuples
    temp_paths: list[str] = []     # files to delete after this batch uploads
    copy_reverse = (order == "oldest")
    copy_min_id = 0 if rebuild_cache else offset_id

    async def _flush_copy_batch():
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
                if delay:
                    await asyncio.sleep(delay)
        if copy_pending:
            cache.mark_many(copy_pending)
            copy_pending.clear()
        # PERMANENTLY delete every downloaded temp file for this batch.
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
    return count, max_id, run_forwarded
