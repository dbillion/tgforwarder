"""Telegram client session + entity resolution."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv, find_dotenv
from telethon import TelegramClient

DEFAULT_SESSION = "forwarder_session1"


def load_project_env() -> None:
    """Load .env robustly regardless of the current working directory.

    The CLI was previously calling load_dotenv(Path(".env")), which only looks in
    the CWD. When `tgf` is invoked from another directory (or as an installed
    console script), that resolves to the wrong path and the API credentials are
    never loaded -> "Set TELEGRAM_API_ID and TELEGRAM_API_HASH" error.

    Resolution order:
      1. .env next to the package (repo root) and a couple of parents
      2. .env in the CWD (existing workflow)
      3. dotenv's own upward walk from CWD
    Values are stripped of surrounding whitespace as a belt-and-suspenders measure.
    """
    candidates = []
    try:
        pkg_root = Path(__file__).resolve().parent.parent  # .../tgforwarder -> repo
        candidates.append(pkg_root / ".env")
        candidates.append(pkg_root.parent / ".env")
        candidates.append(pkg_root.parent.parent / ".env")
    except Exception:
        pass
    candidates.append(Path.cwd() / ".env")

    loaded_any = False
    for c in candidates:
        if c.exists():
            load_dotenv(c, override=False)
            loaded_any = True
    if not loaded_any:
        # Last resort: dotenv's upward search from CWD.
        load_dotenv(find_dotenv(usecwd=True, raise_error_if_not_found=False), override=False)

    # Defensive whitespace strip for credential-style variables.
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TG_SESSION_NAME",
                "SOURCE_CHANNELS", "TARGET_CHANNELS"):
        if key in os.environ and os.environ[key] != os.environ[key].strip():
            os.environ[key] = os.environ[key].strip()


load_project_env()

# Device fingerprint (matches tg-cli style) so the session looks like a real client.
_DEVICE_MODEL = "Desktop"
_SYSTEM_VERSION = "macOS 15.3"
_APP_VERSION = "5.12.1"


def get_api_id() -> int:
    raw = (os.environ.get("TELEGRAM_API_ID", "") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def get_api_hash() -> str:
    return (os.environ.get("TELEGRAM_API_HASH", "") or "").strip()


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
    """Resolve a channel/user from a name, @handle, or numeric ID (handles -100 prefix).

    Falls back to a cached dialog peer: deleted-account chats (PeerUser with no
    server entity) can't be resolved by ID, but the session's cached InputPeer
    still works for reading/forwarding.
    """
    is_numeric = str(name).replace("-", "").isdigit()
    if is_numeric:
        clean = str(name).replace("-100", "")
        for fmt in (int(name), int(f"-100{clean}"), int(clean)):
            try:
                return await client.get_entity(fmt)
            except (ValueError, Exception):
                continue
        # Fallback: find a cached dialog peer with this id (deleted accounts).
        peer = await _cached_dialog_peer(client, int(clean))
        if peer is not None:
            return peer
    for candidate in (name, f"@{name}"):
        try:
            return await client.get_entity(candidate)
        except ValueError:
            continue
    raise ValueError(f"Could not find channel/user: {name}")


async def _cached_dialog_peer(client: TelegramClient, user_id: int):
    """Return an InputPeer for a dialog cached in the session (deleted-account safe)."""
    try:
        async for d in client.iter_dialogs(limit=1000):
            ent = d.entity
            if getattr(ent, "id", None) == user_id:
                try:
                    return await client.get_input_entity(ent)
                except Exception:
                    return ent
    except Exception:
        return None
    return None
