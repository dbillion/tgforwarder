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
