"""tgforwarder — Telegram MTProto media-forwarder with OCR + local-first cache.

Refactored from the monolithic telbot.py/bota.py into installable modules,
following patterns learned from jackwener/tg-cli (local-first SQLite, Click CLI,
structured --json/--yaml output, externalized rate-limiting).
"""
from __future__ import annotations

__version__ = "0.1.0"
