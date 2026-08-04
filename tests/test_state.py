"""Unit tests for tgforwarder.state (resume persistence)."""
from __future__ import annotations

from tgforwarder import state


def test_state_roundtrip(tmp_path):
    p = tmp_path / "st.json"
    st = state.load_state(p)
    assert state.last_id_for(st, "src1") == 0
    state.set_progress(st, "src1", 555, direction="oldest")
    state.save_state(st, p)
    st2 = state.load_state(p)
    assert state.last_id_for(st2, "src1") == 555
    assert state.direction_for(st2, "src1") == "oldest"
