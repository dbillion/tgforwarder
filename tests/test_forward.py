"""Unit tests for tgforwarder.forward._suggested_name (OCR filename suggestion)."""
from __future__ import annotations

from tgforwarder import forward


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
