"""Unit tests for tgforwarder.report.ForwardLogger (scales to 5000+ via deque + Counter)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tgforwarder.report import ForwardLogger


def test_logger_counts_and_types():
    now = datetime.now(timezone.utc)
    l = ForwardLogger()
    assert l.count() == 0
    l.record("a.png", now)
    l.record("b.pdf", now)
    l.record("c.png", now)
    assert l.count() == 3
    assert l.by_type() == {"png": 2, "pdf": 1}


def test_logger_recent_window_filters_old():
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=10)
    l = ForwardLogger()
    l.record("old.pdf", old)
    l.record("new.png", now)
    recent = l.recent_window(minutes=5)
    assert len(recent) == 1
    assert recent[0] == "new.png"


def test_logger_caps_names_memory():
    now = datetime.now(timezone.utc)
    l = ForwardLogger()
    for i in range(1000):
        l.record(f"f{i}.png", now)
    # total keeps counting; stored names capped at MAX_NAMES (50)
    assert l.count() == 1000
    assert len(l._names) == 50
