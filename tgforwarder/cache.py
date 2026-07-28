"""Local-first SQLite cache for forwarded-message history and chat metadata.

Mirrors tg-cli's design: sync once, read many. Persists forward history so
restarts resume without re-sending, and supports the usefulness scorer.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def default_db_path() -> Path:
    data_dir = Path(os.environ.get("DATA_DIR", Path.home() / ".local/share/tg-cli"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "forwarder.db"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS forwarded (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     INTEGER NOT NULL,
    source_msg_id INTEGER NOT NULL,
    target_id     INTEGER NOT NULL,
    target_msg_id INTEGER,
    file_name     TEXT,
    timestamp     TEXT NOT NULL,
    status        TEXT DEFAULT 'ok',
    UNIQUE(source_id, source_msg_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_forwarded_src ON forwarded(source_id, source_msg_id);
"""


class ForwardCache:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def load_done_set(self, source_id: int, target_id: int) -> set[int]:
        """Bulk-load already-forwarded source_msg_ids into an in-memory set (O(1) lookups).

        For 5000+ files this avoids a per-message SQL round-trip.
        """
        rows = self.conn.execute(
            "SELECT source_msg_id FROM forwarded WHERE source_id=? AND target_id=?",
            (source_id, target_id),
        ).fetchall()
        return {r[0] for r in rows}

    def mark_many(self, rows: list[dict]) -> None:
        """Batched insert via executemany (one transaction). O(n) total, not O(n) commits."""
        if not rows:
            return
        data = [
            (
                r["source_id"], r["source_msg_id"], r["target_id"],
                r.get("target_msg_id"), r.get("file_name"),
                datetime.now(timezone.utc).isoformat(), r.get("status", "ok"),
            )
            for r in rows
        ]
        self.conn.executemany(
            """INSERT OR REPLACE INTO forwarded
               (source_id, source_msg_id, target_id, target_msg_id, file_name, timestamp, status)
               VALUES (?,?,?,?,?,?,?)""",
            data,
        )
        self.conn.commit()

    def is_done(self, source_id: int, source_msg_id: int, target_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM forwarded WHERE source_id=? AND source_msg_id=? AND target_id=?",
            (source_id, source_msg_id, target_id),
        ).fetchone()
        return row is not None

    def mark(
        self,
        *,
        source_id: int,
        source_msg_id: int,
        target_id: int,
        target_msg_id: int | None = None,
        file_name: str | None = None,
        status: str = "ok",
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO forwarded
               (source_id, source_msg_id, target_id, target_msg_id, file_name, timestamp, status)
               VALUES (?,?,?,?,?,?,?)""",
            (
                source_id,
                source_msg_id,
                target_id,
                target_msg_id,
                file_name,
                datetime.now(timezone.utc).isoformat(),
                status,
            ),
        )
        self.conn.commit()

    def rebuild_done_set(self, source_id: int, target_id: int, delivered_ids: set[int]) -> int:
        """Replace the cached 'done' set for a (source, target) pair with GROUND TRUTH.

        The normal mark_many path can mark a message 'done' when forward_messages
        returns truthy even if it never actually persisted (common with deleted-account
        peers). That leaves the cache inflated and causes the forwarder to skip real
        messages forever. This method overwrites the pair's rows with the set of
        source_msg_ids that were VERIFIED to exist in the target (e.g. by scanning
        Saved Messages for saved_from_msg_id). Use before a forward run to make dedup
        honest.
        """
        cur = self.conn.execute(
            "DELETE FROM forwarded WHERE source_id=? AND target_id=?",
            (source_id, target_id),
        )
        deleted = cur.rowcount
        if delivered_ids:
            ts = datetime.now(timezone.utc).isoformat()
            self.conn.executemany(
                """INSERT INTO forwarded
                   (source_id, source_msg_id, target_id, target_msg_id, file_name, timestamp, status)
                   VALUES (?,?,?,?,?,?,?)""",
                [
                    (source_id, mid, target_id, None, None, ts, "ok")
                    for mid in delivered_ids
                ],
            )
        self.conn.commit()
        return deleted

    def stats(self) -> dict:
        ok = self.conn.execute("SELECT COUNT(*) FROM forwarded WHERE status='ok'").fetchone()[0]
        failed = self.conn.execute(
            "SELECT COUNT(*) FROM forwarded WHERE status='failed'"
        ).fetchone()[0]
        return {"forwarded": ok, "failed": failed}

    def close(self) -> None:
        self.conn.close()

    # ---- legacy JSON compatibility (read old forward_history.json if present) ----
    @staticmethod
    def load_legacy_json(path: str = "forward_history.json") -> dict:
        p = Path(path)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
