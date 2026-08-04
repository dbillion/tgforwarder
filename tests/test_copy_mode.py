"""Unit tests for tgforwarder COPY-mode pipeline (no Telegram, no network).

Written as plain (sync) functions that drive the coroutine via asyncio.run,
so no pytest-asyncio dependency is required.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tgforwarder.cache import ForwardCache
from tgforwarder.copy_mode import run_copy_mode
from tgforwarder.report import ForwardLogger


class _StubMsg:
    """Minimal stand-in for a Telethon Message with media + caption."""
    def __init__(self, mid, text, media):
        self.id = mid
        self.text = text
        self.message = text
        self.caption = text
        self.media = media


class _StubMedia:
    """A media object whose .document carries a filename attribute (matches forward.original_filename)."""
    def __init__(self, size=10, name="media.bin"):
        self.document = SimpleNamespace(size=size, attributes=[SimpleNamespace(file_name=name)])


class _StubClient:
    """Async stub: yields canned messages, records send_message calls, no real I/O."""
    def __init__(self, messages, *, send_returns_id_start=9000):
        self._messages = list(messages)
        self.sent = []          # (target_id, text, file) tuples
        self.send_id = send_returns_id_start
        self.downloaded = []

    def iter_messages(self, src, *, min_id=0, reverse=False):
        async def gen():
            for m in self._messages:
                if m.id >= min_id:
                    yield m
        return gen()

    async def send_message(self, target, *, message, file=None):
        self.send_id += 1
        self.sent.append((target.id, message, file))
        return SimpleNamespace(id=self.send_id)

    async def download_media(self, msg, *, file):
        # No real disk write; just record the requested path.
        self.downloaded.append(file)
        return file


def _src():
    return SimpleNamespace(id=-1001961116802, title="src")


def _tgt(tid):
    return SimpleNamespace(id=tid, title=f"tgt{tid}")


def _msg(mid, text, media=None):
    return _StubMsg(mid, text, media)


def test_copy_mode_reposts_each_unique_msg_to_every_target(tmp_path):
    src = _src()
    t1, t2 = _tgt(11), _tgt(12)
    cache = ForwardCache(tmp_path / "c.db")
    logger = ForwardLogger()
    msgs = [_msg(1, "hello"), _msg(2, "world"), _msg(3, "again")]
    done = {t1.id: set(), t2.id: set()}

    count, max_id, run_forwarded = asyncio.run(run_copy_mode(
        _StubClient(msgs), src, [t1, t2], order="oldest", rebuild_cache=False,
        offset_id=0, batch_size=25, delay=0, limit=50, process_all=False,
        done_by_target=done, cache=cache, logger=logger,
        count=0, max_id=0, run_forwarded=0,
    ))

    assert count == 3
    assert max_id == 3
    assert run_forwarded == 6          # 3 msgs x 2 targets
    # Re-run the same stub fresh to count its sends (the earlier instance already mutated).
    fresh = _StubClient(msgs)
    asyncio.run(run_copy_mode(
        fresh, src, [t1, t2], order="oldest", rebuild_cache=False,
        offset_id=0, batch_size=25, delay=0, limit=50, process_all=False,
        done_by_target={t1.id: set(), t2.id: set()}, cache=ForwardCache(tmp_path / "c_r.db"),
        logger=ForwardLogger(), count=0, max_id=0, run_forwarded=0,
    ))
    assert len(fresh.sent) == 6        # each target received all 3
    cache.close()


def test_copy_mode_dedups_by_content_hash_within_target(tmp_path):
    src = _src()
    t1 = _tgt(11)
    cache = ForwardCache(tmp_path / "c2.db")
    logger = ForwardLogger()
    # Two messages with identical text => identical content hash => only first delivered.
    msgs = [_msg(1, "dup"), _msg(2, "dup")]
    done = {t1.id: set()}

    client = _StubClient(msgs)
    _, _, run_forwarded = asyncio.run(run_copy_mode(
        client, src, [t1], order="oldest", rebuild_cache=False,
        offset_id=0, batch_size=25, delay=0, limit=50, process_all=False,
        done_by_target=done, cache=cache, logger=logger,
        count=0, max_id=0, run_forwarded=0,
    ))

    assert run_forwarded == 1, "duplicate content hash must not be re-posted to same target"
    assert len(client.sent) == 1
    cache.close()


def test_copy_mode_respects_limit(tmp_path):
    src = _src()
    t1 = _tgt(11)
    cache = ForwardCache(tmp_path / "c3.db")
    logger = ForwardLogger()
    msgs = [_msg(i, f"m{i}") for i in range(1, 20)]
    done = {t1.id: set()}

    client = _StubClient(msgs)
    count, _, _ = asyncio.run(run_copy_mode(
        client, src, [t1], order="oldest", rebuild_cache=False,
        offset_id=0, batch_size=25, delay=0, limit=5, process_all=False,
        done_by_target=done, cache=cache, logger=logger,
        count=0, max_id=0, run_forwarded=0,
    ))

    assert count == 5, "limit must cap processed messages"
    cache.close()


def test_copy_mode_skips_non_media_noop(tmp_path):
    src = _src()
    t1 = _tgt(11)
    cache = ForwardCache(tmp_path / "c4.db")
    logger = ForwardLogger()
    # Only one message actually has media; verify download_media was called and the
    # file path flows to send_message (exercising the media branch without real disk).
    msgs = [_msg(1, "has-media", _StubMedia(size=42))]
    done = {t1.id: set()}

    client = _StubClient(msgs)
    asyncio.run(run_copy_mode(
        client, src, [t1], order="oldest", rebuild_cache=False,
        offset_id=0, batch_size=25, delay=0, limit=50, process_all=False,
        done_by_target=done, cache=cache, logger=logger,
        count=0, max_id=0, run_forwarded=0,
    ))

    assert client.downloaded, "media message should be downloaded"
    assert client.sent[0][2] is not None, "send_message should carry the media file path"
    cache.close()
