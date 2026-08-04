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


def _repo_env_path() -> Path:
    """Best-effort path to the repo .env (next to the package), for saving creds."""
    try:
        return Path(__file__).resolve().parent.parent / ".env"
    except Exception:
        return Path.cwd() / ".env"


def _persist_creds(api_id_str: str, api_hash: str) -> None:
    """Append/fill TELEGRAM_API_ID / TELEGRAM_API_HASH in the repo .env so the user
    is not prompted again. Never echoes the values. Safe no-op if .env is unwritable."""
    import re as _re

    p = _repo_env_path()
    try:
        text = p.read_text() if p.exists() else ""
        # Replace existing keys if present, else append.
        def _set(text: str, key: str, val: str) -> str:
            pattern = _re.compile(rf"^{key}=.*$", _re.MULTILINE)
            line = f"{key}={val}"
            if pattern.search(text):
                return pattern.sub(lambda m: line, text)
            return (text.rstrip() + "\n" + line + "\n")

        text = _set(text, "TELEGRAM_API_ID", api_id_str.strip())
        text = _set(text, "TELEGRAM_API_HASH", api_hash.strip())
        p.write_text(text)
    except Exception:
        # Best-effort only; env vars are already set for this run regardless.
        pass


def get_api_id() -> int:
    raw = (os.environ.get("TELEGRAM_API_ID", "") or "").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def get_api_hash() -> str:
    return (os.environ.get("TELEGRAM_API_HASH", "") or "").strip()


def ensure_credentials() -> None:
    """Interactively prompt for missing API credentials and persist them to .env.

    Replaces the old hard failure ('Set TELEGRAM_API_ID ...'). Works on any OS
    (uses click, an existing dependency) and from any invocation path: the user is
    prompted at most once, then creds are saved so subsequent runs are silent.
    """
    import click

    api_id = get_api_id()
    api_hash = get_api_hash()
    if api_id and api_hash:
        return
    # Re-import here to avoid a hard dependency surprise if click is unavailable.
    console = __import__("rich.console", fromlist=["Console"]).Console()
    console.print(
        "[yellow]🔑 Telegram API credentials not found in environment/.env.[/yellow]\n"
        "   Get them free at https://my.telegram.org (API development tools)."
    )
    if not api_id:
        while True:
            entered = click.prompt("TELEGRAM_API_ID", default="", type=str).strip()
            if entered and entered.isdigit():
                os.environ["TELEGRAM_API_ID"] = entered
                api_id = int(entered)
                break
            console.print("[red]Please enter a numeric API id.[/red]")
    if not api_hash:
        api_hash = click.prompt("TELEGRAM_API_HASH", default="", type=str).strip()
        os.environ["TELEGRAM_API_HASH"] = api_hash
    # Persist so we don't ask again (best-effort; never printed).
    _persist_creds(str(os.environ.get("TELEGRAM_API_ID", "")), os.environ.get("TELEGRAM_API_HASH", ""))


def make_client(session: str | None = None) -> TelegramClient:
    """Create a Telethon client. Session name defaults to the existing forwarder session.

    If TELEGRAM_API_ID / TELEGRAM_API_HASH are missing, the user is prompted
    interactively (and the values are saved to .env for future runs).
    """
    ensure_credentials()
    api_id = get_api_id()
    api_hash = get_api_hash()
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
