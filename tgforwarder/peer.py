"""Forwarding primitives for tgforwarder — pure pipeline logic (no Click).

Extracted from the monolithic cli.py so the CLI layer stays a thin option→call
wrapper. Everything here is importable and unit-testable without Telegram.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import AsyncIterator

from rich.console import Console
from telethon import utils
from telethon.tl.custom import Message

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


async def iter_undone(client, src, done_sets: list[set[int]], *, order: str, min_id: int = 0) -> AsyncIterator[WorkItem]:
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
    """Collect saved_from_msg_id for our source, scanning NEWEST-first and stopping on a cold streak.

    Telethon/MTProto has NO server-side filter for `saved_from_peer` (probed: from_user
    returns 0 because a forwarded copy's sender is you/bot, not the source). So we must
    fetch message objects — but we can bound the cost: forwards from a source cluster near
    when they arrived, so once we hit `cold_after` consecutive non-matches from the top we
    stop. This turns a 14k download into a few-hundred-message window on steady state.
    """
    from .forward import original_filename

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


def content_hash_of(msg) -> str:
    """Stable content key for COPY mode (no saved_from_peer available).

    Uses text + media descriptor (name+size+type). Media bytes are NOT hashed here
    (that would require downloading); name+size is a stable, collision-resistant key
    for re-post dedup, and we only download when we actually re-post.
    """
    from .forward import original_filename

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
    from .forward import original_filename

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
