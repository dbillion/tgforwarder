"""Stubbed integration test for commands.forward_run (no Telegram, no network).

Monkeypatches make_client/resolve_entity with an in-memory async stub so the FULL
forward pipeline runs: ground-truth cache rebuild -> dedup producer -> batched
forward + verify -> persist -> final verification report -> resume-state save.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tgforwarder import state
from tgforwarder.cache import ForwardCache
from tgforwarder.commands import forward_run


class _Msg:
    def __init__(self, mid, text="", fwd_from=None):
        self.id = mid
        self.text = text
        self.message = text
        self.caption = text
        self.media = None
        self.fwd_from = fwd_from
        self.chat_id = -1001961116802


class _FwdHeader:
    def __init__(self, saved_from_peer_id, saved_from_msg_id):
        from telethon.tl.types import PeerChannel
        self.saved_from_peer = PeerChannel(saved_from_peer_id)
        self.saved_from_msg_id = saved_from_msg_id


class _StubClient:
    """In-memory stand-in for TelegramClient.

    `source_msgs` are the source channel's messages. `target_msgs` are what already
    sits in the (single) target — used to exercise the ground-truth rebuild path.
    """
    def __init__(self, source_msgs, target_msgs, total):
        self._source = list(source_msgs)
        self._target = list(target_msgs)
        self._total = total
        self.forwarded = []   # list of (target, [msg ids]) actually sent

    async def start(self):
        return None

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        return None

    async def get_messages(self, target, *, ids=None, limit=None):
        # Verify step asks for returned ids; echo them back as "present" (verified).
        by_id = {t.id: t for t in self._target}
        return [by_id.get(i, SimpleNamespace(id=i)) for i in (ids or [])]

    def iter_messages(self, chat, *, min_id=0, reverse=False, limit=None):
        async def gen():
            pool = self._source if chat.id == -1001961116802 else self._target
            for m in pool:
                if m.id >= min_id:
                    yield m
        return gen()

    async def forward_messages(self, target, msgs):
        # Simulate delivery: allocate new target-side ids and add to target store.
        sent = []
        for m in msgs:
            new_id = 100000 + len(self._target) + len(sent) + 1
            tm = _Msg(new_id, m.text)
            tm.fwd_from = _FwdHeader(1961116802, m.id)  # mark as from our source
            self._target.append(tm)
            sent.append(SimpleNamespace(id=new_id))
        self.forwarded.append((getattr(target, "id", None), [s.id for s in sent]))
        return sent


def _stub_src(ids):
    return SimpleNamespace(id=-1001961116802, title="src")


def _stub_tgt(tid=-2001):
    return SimpleNamespace(id=tid, title="dst")


def test_forward_run_marks_delivered_and_persists_resume(tmp_path, monkeypatch):
    # 10 source messages, none yet delivered.
    src = [_Msg(i, f"msg {i}") for i in range(1, 11)]
    cache_db = tmp_path / "fwd.db"
    state_db = tmp_path / "st.json"

    # Build one shared stub client (source + single target with nothing delivered).
    client = _StubClient(src, target_msgs=[], total=10)

    monkeypatch.setattr("tgforwarder.commands.make_client", lambda session=None: client)
    async def _resolve(c, name):
        return _stub_src(None) if str(name).startswith("-100") else _stub_tgt()
    monkeypatch.setattr("tgforwarder.commands.resolve_entity", _resolve)

    # Resolve state db path via env so fstate uses tmp.
    monkeypatch.setenv("FORWARD_STATE", str(state_db))
    from tgforwarder import state as fstate
    from tgforwarder import cache as fcache
    monkeypatch.setattr(fstate, "DEFAULT_STATE", state_db)
    monkeypatch.setattr(fcache, "default_db_path", lambda: cache_db)

    asyncio.run(forward_run(
        source="-1001961116802", dest=("-2001",), dl_path=str(tmp_path / "dl"),
        limit=10, process_all=True, order="oldest", session=None, delay=0,
        batch_size=4, resume=False, start=True, rebuild_cache=True,
        force_rebuild=True, quick_rebuild=False, copy_mode=False,
    ))

    # Cache should now report 10 delivered to target -2001.
    c = ForwardCache(cache_db)
    done = c.load_done_set(-1001961116802, -2001)
    assert len(done) == 10, f"expected 10 delivered, got {len(done)}"
    c.close()

    # Resume state persisted: max id = 10.
    st = state.load_state(state_db)
    assert state.last_id_for(st, "-1001961116802") == 10


def test_forward_run_respects_existing_ground_truth(tmp_path, monkeypatch):
    """If 3 of 10 source msgs are already in the target, only the other 7 are forwarded."""
    src = [_Msg(i, f"msg {i}") for i in range(1, 11)]
    # Target already has forwards of source ids 1,2,3.
    target = [_Msg(100 + i, f"msg {i}", fwd_from=_FwdHeader(1961116802, i)) for i in (1, 2, 3)]
    client = _StubClient(src, target_msgs=target, total=10)

    monkeypatch.setattr("tgforwarder.commands.make_client", lambda session=None: client)
    async def _resolve2(c, name):
        return _stub_src(None) if str(name).startswith("-100") else _stub_tgt()
    monkeypatch.setattr("tgforwarder.commands.resolve_entity", _resolve2)
    monkeypatch.setenv("FORWARD_STATE", str(tmp_path / "st.json"))
    from tgforwarder import state as fstate
    from tgforwarder import cache as fcache
    monkeypatch.setattr(fstate, "DEFAULT_STATE", tmp_path / "st.json")
    monkeypatch.setattr(fcache, "default_db_path", lambda: tmp_path / "fwd.db")

    asyncio.run(forward_run(
        source="-1001961116802", dest=("-2001",), dl_path=str(tmp_path / "dl"),
        limit=10, process_all=True, order="oldest", session=None, delay=0,
        batch_size=4, resume=False, start=True, rebuild_cache=True,
        force_rebuild=True, quick_rebuild=False, copy_mode=False,
    ))

    # Only 7 new messages were forwarded (ids 4..10).
    assert len(client.forwarded) >= 1
    total_sent = sum(len(ids) for _, ids in client.forwarded)
    assert total_sent == 7, f"expected 7 newly forwarded, got {total_sent}"
