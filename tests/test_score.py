"""Unit tests for tgforwarder.score.score_chats (usefulness triage)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from tgforwarder import score


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
