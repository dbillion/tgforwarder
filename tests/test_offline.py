"""Unit tests for tgforwarder offline logic (no Telegram, no network).

Covers: OCR filename suggestion, chat usefulness scoring/verdict, cache dedup.
The resolve_entity ID-parsing branch is tested via a stubbed client.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tgforwarder import forward, score
from tgforwarder.cache import ForwardCache


# --------------------------------------------------------------------------
# forward._suggested_name
# --------------------------------------------------------------------------
def test_suggested_name_basic():
    out = forward._suggested_name("Hello World from OCR", ".png")
    assert out == "Hello_World_from_OCR.png"


def test_suggested_name_strips_slashes():
    out = forward._suggested_name("a/b c", ".pdf")
    assert "/" not in out
    assert out == "a_b_c.pdf"


def test_suggested_name_limits_to_five_words():
    out = forward._suggested_name("one two three four five six seven", ".jpg")
    assert out == "one_two_three_four_five.jpg"


# --------------------------------------------------------------------------
# score_chats — verdict + noise denylist + topic filter
# --------------------------------------------------------------------------
def _make_db(path: Path, rows: list[dict]):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT DEFAULT 'telegram',
            chat_id INTEGER NOT NULL,
            chat_name TEXT,
            msg_id INTEGER NOT NULL,
            sender_id INTEGER,
            sender_name TEXT,
            content TEXT,
            timestamp TEXT NOT NULL
        );
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO messages (chat_id, chat_name, msg_id, sender_id, sender_name, content, timestamp)"
            " VALUES (?,?,?,?,?,?,?)",
            (r["chat_id"], r["chat_name"], r["msg_id"], r["sender_id"],
             r.get("sender_name"), r["content"], r["timestamp"]),
        )
    conn.commit()
    conn.close()


def test_score_useful_chat():
    db = Path("__test_useful.db")
    _make_db(db, [
        {"chat_id": 1, "chat_name": "Rust Programming", "msg_id": i, "sender_id": i % 10 + 1,
         "content": "rust tip about ownership", "timestamp": "2026-07-28T09:00:00+00:00"}
        for i in range(1, 51)
    ])
    scored = score.score_chats(str(db), topics="rust", top=5)
    assert scored, "expected at least one scored chat"
    assert scored[0]["chat_name"] == "Rust Programming"
    assert scored[0]["verdict"] in ("USEFUL", "OK")
    db.unlink()


def test_score_noise_denylist_demotes():
    db = Path("__test_noise.db")
    _make_db(db, [
        {"chat_id": 2, "chat_name": "Prayer Hub", "msg_id": i, "sender_id": 1,
         "content": "join our prayer meeting tonight", "timestamp": "2026-07-28T09:00:00+00:00"}
        for i in range(1, 51)
    ])
    scored = score.score_chats(str(db), topics="rust", top=5)
    assert scored[0]["verdict"] == "NOISE", scored[0]
    assert scored[0]["score"] <= 30
    db.unlink()


def test_score_topic_filter_excludes_offtopic():
    db = Path("__test_topic.db")
    _make_db(db, [
        {"chat_id": 3, "chat_name": "Crypto Signals", "msg_id": i, "sender_id": i % 3 + 1,
         "content": "buy this coin now airdrop", "timestamp": "2026-07-28T09:00:00+00:00"}
        for i in range(1, 51)
    ])
    # With a dev topic filter, an off-topic chat must be demoted below threshold
    scored = score.score_chats(str(db), topics="java,rust,devops,ai", top=5)
    assert scored[0]["score"] <= 30, scored[0]
    db.unlink()


def test_score_old_chat_decay():
    db = Path("__test_old.db")
    _make_db(db, [
        {"chat_id": 4, "chat_name": "Stale Group", "msg_id": i, "sender_id": i % 5 + 1,
         "content": "rust devops ai course", "timestamp": "2026-01-01T09:00:00+00:00"}
        for i in range(1, 51)
    ])
    scored = score.score_chats(str(db), topics="rust,devops,ai", top=5)
    # 6+ months old -> recency near 0 -> should not be USEFUL
    assert scored[0]["verdict"] != "USEFUL", scored[0]
    db.unlink()


# --------------------------------------------------------------------------
# cache.ForwardCache dedup
# --------------------------------------------------------------------------
def test_cache_marks_and_detects_done(tmp_path):
    db = tmp_path / "fwd.db"
    c = ForwardCache(db)
    assert not c.is_done(10, 100, 20)
    c.mark(source_id=10, source_msg_id=100, target_id=20, target_msg_id=999)
    assert c.is_done(10, 100, 20)
    assert c.stats()["forwarded"] == 1
    c.close()


def test_cache_no_duplicate_on_repeat(tmp_path):
    db = tmp_path / "fwd2.db"
    c = ForwardCache(db)
    c.mark(source_id=10, source_msg_id=100, target_id=20, target_msg_id=1)
    c.mark(source_id=10, source_msg_id=100, target_id=20, target_msg_id=2)  # UNIQUE ignored
    assert c.stats()["forwarded"] == 1
    c.close()


def test_cache_bulk_load_and_mark_many(tmp_path):
    db = tmp_path / "fwd3.db"
    c = ForwardCache(db)
    # Simulate 5000 prior forwards in one batch write.
    rows = [{"source_id": 10, "source_msg_id": i, "target_id": 20} for i in range(1, 5001)]
    c.mark_many(rows)
    c.close()
    c2 = ForwardCache(db)
    done = c2.load_done_set(10, 20)
    assert len(done) == 5000
    assert 1 in done and 5000 in done
    assert c2.is_done(10, 2500, 20) is True
    c2.close()


def test_cache_rebuild_done_set_replaces_inflated_cache(tmp_path):
    """The cache must be replaceable with GROUND TRUTH (what's actually in the target).

    Regression test: without rebuild, a cache inflated by false 'done' marks would
    make the forwarder skip real messages forever. rebuild_done_set must overwrite
    the pair's rows with exactly the delivered set.
    """
    db = tmp_path / "fwd4.db"
    c = ForwardCache(db)
    # Inflated: 100 ids marked done, but only 3 actually delivered.
    c.mark_many([{"source_id": 10, "source_msg_id": i, "target_id": 20} for i in range(1, 101)])
    assert c.stats()["forwarded"] == 100
    # Ground truth from the target: only ids 5, 6, 7 really arrived.
    removed = c.rebuild_done_set(10, 20, {5, 6, 7})
    assert removed == 100
    assert c.stats()["forwarded"] == 3
    done = c.load_done_set(10, 20)
    assert done == {5, 6, 7}
    assert not c.is_done(10, 1, 20)   # false positive gone
    assert c.is_done(10, 5, 20)       # truth kept
    c.close()


def test_cache_migration_adds_content_hash_column(tmp_path):
    """An old DB created WITHOUT content_hash must be migrated transparently on open.
    Regression for the sqlite3.OperationalError: no such column: content_hash crash."""
    import sqlite3
    db = tmp_path / "fwd_old.db"
    # Create a DB with the OLD schema (no content_hash column).
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE forwarded (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            source_msg_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            target_msg_id INTEGER,
            file_name TEXT,
            timestamp TEXT NOT NULL,
            status TEXT DEFAULT 'ok',
            UNIQUE(source_id, source_msg_id, target_id)
        );
    """)
    conn.execute("INSERT INTO forwarded (source_id, source_msg_id, target_id, timestamp) VALUES (1, 5, 2, '2026-01-01')")
    conn.commit(); conn.close()
    # Opening with ForwardCache must migrate: add content_hash without error.
    c = ForwardCache(db)
    # mark_many with content_hash must now work (proves the column exists).
    c.mark_many([{"source_id": 1, "source_msg_id": 6, "target_id": 2, "content_hash": "abc123"}])
    hashes = c.load_done_hashes(1, 2)
    assert hashes == {"abc123"}
    # Re-opening must be idempotent (migration safe to run again).
    c.close()
    c2 = ForwardCache(db)
    assert c2.load_done_hashes(1, 2) == {"abc123"}
    c2.close()


def test_cache_hash_dedup_per_target(tmp_path):
    """COPY mode dedup: same content hash in two targets, different source ids."""
    db = tmp_path / "fwdh.db"
    c = ForwardCache(db)
    c.mark_many([
        {"source_id": 7, "source_msg_id": 100, "target_id": 11, "content_hash": "H1"},
        {"source_id": 7, "source_msg_id": 100, "target_id": 12, "content_hash": "H1"},
    ])
    assert c.load_done_hashes(7, 11) == {"H1"}
    assert c.load_done_hashes(7, 12) == {"H1"}
    # A different msg id with a new hash is NOT yet done for target 11.
    assert c.load_done_hashes(7, 11) == {"H1"}  # only H1
    c.close()


# --------------------------------------------------------------------------
# state (resume persistence)
# --------------------------------------------------------------------------
def test_state_roundtrip(tmp_path):
    from tgforwarder import state
    p = tmp_path / "st.json"
    st = state.load_state(p)
    assert state.last_id_for(st, "src1") == 0
    state.set_progress(st, "src1", 555, direction="oldest")
    state.save_state(st, p)
    st2 = state.load_state(p)
    assert state.last_id_for(st2, "src1") == 555
    assert state.direction_for(st2, "src1") == "oldest"


# --------------------------------------------------------------------------
# report.ForwardLogger (scales to 5000+ via deque + Counter)
# --------------------------------------------------------------------------
def test_logger_counts_and_types():
    from tgforwarder.report import ForwardLogger
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    l = ForwardLogger()
    assert l.count() == 0
    l.record("a.png", now)
    l.record("b.pdf", now)
    l.record("c.png", now)
    assert l.count() == 3
    assert l.by_type() == {"png": 2, "pdf": 1}


def test_logger_recent_window_filters_old():
    from tgforwarder.report import ForwardLogger
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=10)
    l = ForwardLogger()
    l.record("old.pdf", old)
    l.record("new.png", now)
    recent = l.recent_window(minutes=5)
    assert len(recent) == 1
    assert recent[0] == "new.png"


def test_logger_caps_names_memory():
    from tgforwarder.report import ForwardLogger
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    l = ForwardLogger()
    for i in range(1000):
        l.record(f"f{i}.png", now)
    # total keeps counting; stored names capped at MAX_NAMES (50)
    assert l.count() == 1000
    assert len(l._names) == 50


# --------------------------------------------------------------------------
# peer._is_from_source — channel vs user peer matching
# --------------------------------------------------------------------------
def test_is_from_source_matches_channel_and_user():
    from tgforwarder.peer import _is_from_source
    from telethon.tl.types import PeerChannel, PeerUser, MessageFwdHeader
    from datetime import datetime

    chan_src = -1001961116802  # repo's SOURCE_CHANNELS (a channel -> PeerChannel)
    user_src = 558372819       # repo's other source (a user -> PeerUser)
    fwd_chan = MessageFwdHeader(date=datetime.now(), saved_from_peer=PeerChannel(1961116802), saved_from_msg_id=1)
    fwd_user = MessageFwdHeader(date=datetime.now(), saved_from_peer=PeerUser(558372819), saved_from_msg_id=1)
    # Regression: a channel-sourced forward must match a channel source id.
    # Before the fix this returned False (PeerUser(src.id) != PeerChannel(...)),
    # which silently broke dedup rebuild / verification / dedupe for channels.
    assert _is_from_source(fwd_chan, chan_src) is True
    assert _is_from_source(fwd_user, user_src) is True
    # Wrong source must not match.
    assert _is_from_source(fwd_chan, user_src) is False
    # No forward header must not match.
    assert _is_from_source(None, chan_src) is False


# --------------------------------------------------------------------------
# client.load_project_env — .env must load regardless of CWD
# --------------------------------------------------------------------------
def test_load_project_env_finds_repo_dotenv_from_foreign_cwd(monkeypatch, tmp_path):
    """Regression: `tgf` invoked from a non-repo dir previously failed to load
    .env (it used load_dotenv(Path('.env')) = CWD-relative), so API creds were
    missing and the CLI printed 'Set TELEGRAM_API_ID ...'. The loader must resolve
    .env relative to the package/repo, not the caller's CWD.
    """
    import os
    from tgforwarder import client as cl

    repo_env = Path(cl.__file__).resolve().parent.parent / ".env"  # .../tgforwarder/..
    assert repo_env.exists(), "repo .env must exist for this test"
    monkeypatch.chdir(tmp_path)              # simulate running from a foreign dir
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)   # ensure not inherited
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    cl.load_project_env()
    assert cl.get_api_id() != 0, "API id should load from repo .env even from foreign CWD"
    assert cl.get_api_hash(), "API hash should load from repo .env even from foreign CWD"


def test_ensure_credentials_prompts_when_missing(monkeypatch, tmp_path):
    """Regression: when creds are absent, make_client must prompt instead of
    raising SystemExit('Set TELEGRAM_API_ID ...'). Simulates a foreign CWD with no
    .env at all, then feeds canned answers via click.prompt."""
    import os
    from unittest import mock
    from tgforwarder import client as cl

    monkeypatch.chdir(tmp_path)
    # Force the "no creds anywhere" path: clear env AND neutralize .env discovery.
    for v in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TG_API_ID", "TG_API_HASH"):
        monkeypatch.delenv(v, raising=False)
        os.environ.pop(v, None)
    monkeypatch.setattr(cl, "load_project_env", lambda: None)  # pretend no .env found
    assert cl.get_api_id() == 0, "precondition: creds missing"

    answers = iter(["28150103", "deadbeefdeadbeefdeadbeefdeadbeef"])
    import click as _click
    with mock.patch.object(_click, "prompt", side_effect=lambda *a, **k: next(answers)):
        # Must NOT raise SystemExit now.
        cl.ensure_credentials()
    assert cl.get_api_id() == 28150103, "prompted API id should be set"
    assert cl.get_api_hash() == "deadbeefdeadbeefdeadbeefdeadbeef", "prompted hash should be set"
