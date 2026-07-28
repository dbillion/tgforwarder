"""Resume-state persistence for the forwarder (offline-testable).

Stores the last forwarded message id per source channel so `--resume`
continues from where a previous run stopped, and `--start` ignores it.
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


def last_id_for(state: dict, source: str) -> int:
    return int(state.get("sources", {}).get(str(source), {}).get("last_message_id", 0))


def set_last_id(state: dict, source: str, msg_id: int) -> dict:
    state.setdefault("sources", {})
    state["sources"][str(source)] = {"last_message_id": msg_id}
    return state
