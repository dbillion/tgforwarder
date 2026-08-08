"""Unit tests for tgforwarder.client env loading + credential prompting."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest import mock

import pytest

from tgforwarder import client as cl


def test_load_project_env_finds_repo_dotenv_from_foreign_cwd(monkeypatch, tmp_path):
    """Regression: `tgf` invoked from a non-repo dir previously failed to load
    .env (it used load_dotenv(Path('.env')) = CWD-relative), so API creds were
    missing and the CLI printed 'Set TELEGRAM_API_ID ...'. The loader must resolve
    .env relative to the package/repo, not the caller's CWD.
    """
    repo_env = Path(cl.__file__).resolve().parent.parent / ".env"  # .../tgforwarder/..
    assert repo_env.exists(), "repo .env must exist for this test"
    monkeypatch.chdir(tmp_path)              # simulate running from a foreign dir
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)   # ensure not inherited
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    cl.load_project_env()
    assert cl.get_api_id() != 0, "API id should load from repo .env even from foreign CWD"
    assert cl.get_api_hash(), "API hash should load from repo .env even from foreign CWD"


def test_ensure_credentials_prompts_when_missing(monkeypatch, tmp_path):
    """Regression: when creds are absent, make_client must prompt instead of
    raising SystemExit('Set TELEGRAM_API_ID ...'). Simulates a foreign CWD with no
    .env at all, then feeds canned answers via click.prompt.
    """
    monkeypatch.chdir(tmp_path)
    # Force the "no creds anywhere" path: clear env AND neutralize .env discovery.
    for v in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TG_API_ID", "TG_API_HASH"):
        monkeypatch.delenv(v, raising=False)
        os.environ.pop(v, None)
    monkeypatch.setattr(cl, "load_project_env", lambda: None)  # pretend no .env found
    assert cl.get_api_id() == 0, "precondition: creds missing"

    answers = iter(["28150103", "deadbeefdeadbeefdeadbeefdeadbeef"])
    import click as _click
    with mock.patch.object(_click, "prompt", side_effect=lambda *a, **k: next(answers)):
        # Guard: never let the test persist its canned creds to the REAL repo .env.
        monkeypatch.setattr(cl, "_persist_creds", lambda *a, **k: None)
        # Must NOT raise SystemExit now.
        cl.ensure_credentials()
    assert cl.get_api_id() == 28150103, "prompted API id should be set"
    assert cl.get_api_hash() == "deadbeefdeadbeefdeadbeefdeadbeef", "prompted hash should be set"


class _FakeUnauthorizedClient:
    """Connects fine but reports an unauthorized session (the actual bug: a
    session file exists and connects, but was never logged in / got revoked)."""
    def __init__(self):
        self.session = mock.Mock(filename="/tmp/fake_session")
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return False

    async def disconnect(self):
        self.disconnected = True


class _FakeAuthorizedClient(_FakeUnauthorizedClient):
    async def is_user_authorized(self):
        return True


def test_connect_authorized_raises_clean_error_when_not_logged_in():
    """Regression: forward/copy/dedupe/test-ocr used to call bare `client.start()`,
    which falls back to Telethon's blocking `input()` phone prompt when the
    session isn't authorized -- hangs/EOFErrors in any non-interactive context
    with no useful message. `connect_authorized` must fail fast with SystemExit
    and point the user at `tgf login`, and must disconnect on the way out.
    """
    fake = _FakeUnauthorizedClient()
    with pytest.raises(SystemExit):
        asyncio.run(cl.connect_authorized(fake))
    assert fake.connected, "should still attempt connect() before checking auth"
    assert fake.disconnected, "should disconnect cleanly rather than leak the connection"


def test_connect_authorized_passes_through_when_logged_in():
    """When the session IS authorized, connect_authorized should just connect
    and return -- no error, no disconnect."""
    fake = _FakeAuthorizedClient()
    asyncio.run(cl.connect_authorized(fake))
    assert fake.connected
    assert not fake.disconnected
