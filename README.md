# tgforwarder

Telegram MTProto media-forwarder (Telethon) with OCR-based file renaming, a
local-first SQLite cache, and chat *usefulness* scoring for research/scraping
triage.

Refactored from monolithic `telbot.py`/`bota.py` following patterns from
[jackwener/tg-cli](https://github.com/jackwener/tg-cli): local-first SQLite,
Click CLI, structured `--json` output, externalized rate-limiting.

## Install (uv)

```bash
cd tgforwarder
uv tool install .          # installs the `tgf` command
# or, for local dev:
uv sync
```

## Configure

```bash
export TELEGRAM_API_ID=...        # from my.telegram.org
export TELEGRAM_API_HASH=...
export TG_SESSION_NAME=forwarder_session1   # reuses your existing user session
```

## Usage

```bash
tgf status                       # check API config
tgf forward --source SRC --target DST --limit 50
tgf test-ocr --source SRC        # OCR the last 3 media messages
tgf score --topic "java,rust,devops,ai" --top 20
tgf score --db ~/.local/share/tg-cli/messages.db --json
```

Requires a logged-in user session (`forwarder_session1.session`). For chat
scoring, first populate the cache with `tg-user sync-all -n 200` (tg-cli).
