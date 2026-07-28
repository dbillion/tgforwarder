"""Resume-state persistence for the forwarder (offline-testable).

Stores the last processed message id AND direction (oldest/newest) per source
so `--resume` continues correctly regardless of forward order.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_STATE = Path(".forward_state.json")


def load_state(path: Path | None = None) -> dict:
    p = path or DEFAULT_STATE
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict, path: Path | None = None) -> None:
    p = path or DEFAULT_STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


def _entry(state: dict, source: str) -> dict:
    return state.setdefault("sources", {}).setdefault(str(source), {})


def last_id_for(state: dict, source: str) -> int:
    return int(_entry(state, source).get("last_message_id", 0))


def direction_for(state: dict, source: str) -> str:
    return _entry(state, source).get("direction", "oldest")


def set_progress(state: dict, source: str, msg_id: int, direction: str = "oldest") -> dict:
    e = _entry(state, source)
    e["last_message_id"] = msg_id
    e["direction"] = direction
    return state
