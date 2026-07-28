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
