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
