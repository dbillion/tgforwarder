"""Telegram client session + entity resolution."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

DEFAULT_SESSION = "forwarder_session1"

# Device fingerprint (matches tg-cli style) so the session looks like a real client.
_DEVICE_MODEL = "Desktop"
_SYSTEM_VERSION = "macOS 15.3"
_APP_VERSION = "5.12.1"


def get_api_id() -> int:
    return int(os.environ.get("TELEGRAM_API_ID", "0"))


def get_api_hash() -> str:
    return os.environ.get("TELEGRAM_API_HASH", "")


def make_client(session: str | None = None) -> TelegramClient:
    """Create a Telethon client. Session name defaults to the existing forwarder session."""
    api_id = get_api_id()
    api_hash = get_api_hash()
    if not api_id or not api_hash:
        raise SystemExit(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in your .env (from my.telegram.org)."
        )
    name = session or os.environ.get("TG_SESSION_NAME", DEFAULT_SESSION)
    # Place session file next to the package data dir for consistency with tg-user.
    data_dir = Path(os.environ.get("DATA_DIR", Path.home() / ".local/share/tg-cli"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(data_dir / name),
        api_id,
        api_hash,
        device_model=_DEVICE_MODEL,
        system_version=_SYSTEM_VERSION,
        app_version=_APP_VERSION,
    )


async def resolve_entity(client: TelegramClient, name: str):
    """Resolve a channel/user from a name, @handle, or numeric ID (handles -100 prefix)."""
    if str(name).replace("-", "").isdigit():
        clean = str(name).replace("-100", "")
        for fmt in (int(name), int(f"-100{clean}"), int(clean)):
            try:
                return await client.get_entity(fmt)
            except (ValueError, Exception):
                continue
    for candidate in (name, f"@{name}"):
        try:
            return await client.get_entity(candidate)
        except ValueError:
            continue
    raise ValueError(f"Could not find channel/user: {name}")
