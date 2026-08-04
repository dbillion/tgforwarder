"""Unit tests for tgforwarder.peer._is_from_source (channel vs user peer matching)."""
from __future__ import annotations

from datetime import datetime

from telethon.tl.types import MessageFwdHeader, PeerChannel, PeerUser

from tgforwarder.peer import _is_from_source


def test_is_from_source_matches_channel_and_user():
    chan_src = -1001961116802  # repo's SOURCE_CHANNELS (a channel -> PeerChannel)
    user_src = 558372819       # repo's other source (a user -> PeerUser)
    fwd_chan = MessageFwdHeader(date=datetime.now(), saved_from_peer=PeerChannel(1961116802), saved_from_msg_id=1)
    fwd_user = MessageFwdHeader(date=datetime.now(), saved_from_peer=PeerUser(558372819), saved_from_msg_id=1)
    # Regression: a channel-sourced forward must match a channel source id.
    # Before the fix this returned False (PeerUser(src.id) != PeerChannel(...)),
    # which silently broke dedup rebuild / verification / dedupe for channels.
    assert _is_from_source(fwd_chan, chan_src) is True
    assert _is_from_source(fwd_user, user_src) is True
    # Wrong source must not match.
    assert _is_from_source(fwd_chan, user_src) is False
    # No forward header must not match.
    assert _is_from_source(None, chan_src) is False
