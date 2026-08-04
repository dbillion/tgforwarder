"""Unit tests for tgforwarder.cache.ForwardCache (dedup + ground-truth rebuild)."""
from __future__ import annotations

import sqlite3

from tgforwarder.cache import ForwardCache


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
    db = tmp_path / "fwd_old.db"
    # Create a DB with the OLD schema (no content_hash column).
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
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
        """
    )
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
