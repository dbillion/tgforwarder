---
name: tgf-telegram-forwarder-setup
description: Setup/operate the tgf Telegram MTProto forwarder CLI.
---

# tgf (tgforwarder) — Telegram MTProto Forwarder Setup & Ops

A modular, `uv`-installable CLI (`tgf`) that forwards media/text from one Telegram
channel to others via MTProto (Telethon), OCR-renames files (Rust **kreuzberg**),
dedups locally (SQLite), and logs an emoji summary.

## Architecture (modular — NO monoliths)
- `tgforwarder/client.py` — TelegramClient session + `resolve_entity` (numeric/-100 ID, @handle, name; deleted-account cached-peer fallback)
- `tgforwarder/cache.py` — `ForwardCache` SQLite dedup; `load_done_set()` (bulk→set), `mark_many()` (batched executemany)
- `tgforwarder/forward.py` — OCR via **kreuzberg** (Rust) primary, Tesseract fallback; `extract_text`
- `tgforwarder/state.py` — resume persistence: `last_message_id` + `direction` per source
- `tgforwarder/report.py` — `ForwardLogger`: deque(maxlen)+Counter+window deque → O(1) at 5000+ files
- `tgforwarder/cli.py` — Click CLI: `forward` (--order oldest|newest, --all, --batch, --dest, --path, --resume/--start), `score`, `test-ocr`, `status`
- `tests/test_offline.py` — 14 offline tests (no network/API)

## Install (one-liners, no clone)
```bash
uv tool install "git+https://github.com/dbillion/tgforwarder.git@v0.1.0"   # pinned release
uvx --from "git+https://github.com/dbillion/tgforwarder.git@v0.1.0" tgf --help  # no-install run
npx skills --skill dbillion/tgforwarder/tgf-agent-install                  # agent skill
```

## Setup (fresh machine)
1. Creds: create `tgforwarder/.env` (gitignored — NEVER commit):
   ```
   TELEGRAM_API_ID=<id>
   TELEGRAM_API_HASH=<hash>
   TG_SESSION_NAME=forwarder_session1
   SOURCE_CHANNELS=-100...
   DEST_CHANNELS=-100...,<YOUR_USER_ID>
   FORWARD_PATH=downloads
   ```
2. Session: need a **user** `.session` (MTProto can't use a bot token).
   Copy an existing logged-in user session to `~/.local/share/tg-cli/forwarder_session1.session`.
3. `uv tool install "git+https://github.com/dbillion/tgforwarder.git@v0.1.0"`

## CRITICAL pitfalls (learned the hard way)
- **Use `setuptools`, NOT hatchling.** Hatchling wheels omitted package `.py` →
  `ModuleNotFoundError` at runtime. pyproject uses setuptools.build_meta.
- **`find_dotenv()` is a trap** — walks UP and finds nearest parent `.env`. Load explicitly:
  `load_dotenv(Path(".env"), override=False)`. Run `tgf` from the project dir.
- **`uv tool install` caches by content hash** — after a build fix:
  `rm -rf dist && uv build --wheel --no-cache && uv tool uninstall tgforwarder && uv tool install . --no-cache`.
- **`min_id` vs `offset_id`:** `iter_messages(offset_id=X)` returns OLDER msgs (<X);
  use `min_id=X` for "resume newer than X". `reverse=True` yields oldest-first.
- **Deleted-account chats:** can't resolve by ID, but the session's cached dialog holds a
  valid InputPeer. `resolve_entity` falls back to scanning `iter_dialogs` for the matching
  `.id` then `get_input_entity(ent)`. Verified fwd from deleted-acct 558372819 → Saved Messages.
- **Batched throughput:** `forward` batches `--batch N` messages into ONE `forward_messages`
  call; `--delay` applies per batch. ~25 files/sec with `--batch 25 --delay 1`.
- **One session at a time** (concurrent sync holds the DB lock).

## OCR: kreuzberg (Rust) is the fast path
- `kreuzberg` = precompiled Rust lib with Python API. `from kreuzberg import extract_file_sync`.
- Falls back to Tesseract if kreuzberg import fails.

## Commands
```bash
tgf status
tgf forward --order oldest --limit 10 --start
tgf forward --resume
tgf forward --source <CH> --dest <UID> --all --batch 25 --delay 1
tgf score --db ~/.local/share/tg-cli/messages.db --topic "rust,devops,ai" --top 5
tgf test-ocr --source <numeric_id>
```
- `test-ocr`/`resolve_entity` need a **numeric channel ID** or `@handle`, NOT a display title.

## Push / share
- Repo pushes fine via `gh` (e.g. `gh repo create tgforwarder --public --push --source .`).
  Do NOT use destructive git actions (no force push, no commit deletion, no reflog tricks).
- Keep `.env`/`.session`/`.db` gitignored; confirm with `git status --short | grep '.env'`.

## Verification checklist
1. `pytest -q` → 14 passed. 2. wheel RECORD contains `tgforwarder/*.py`.
3. `uv tool install . --no-cache` → `tgf --help` works. 4. `tgf status` → "api configured: yes".
5. Live smoke: `tgf forward --start --order oldest --limit 2` forwards + writes state.
6. `git status` shows `.env` UNTRACKED.
