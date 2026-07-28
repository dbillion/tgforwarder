# tgforwarder (`tgf`)

A modular, `uv`-installable **Telegram MTProto media-forwarder** (Telethon) with:

- 📨 **Native forwarding** of messages/media from any channel, group, or **deleted-account
  chat** you still have access to — to one or more destinations (including your **Saved
  Messages**).
- 🔍 **Rust-powered OCR** via [kreuzberg](https://github.com/kreuzberg-ocr/kreuzberg)
  (precompiled Rust library with a Python API) for automatic file renaming.
- 💾 **Local-first SQLite dedup cache** — resume safely, never re-send.
- 📊 **Emoji logging** — count, per-type breakdown, 5-minute window, file names.
- 🧩 **Oldest / newest ordering**, interactive menu, and a `--all` mode that scales to
  5000+ files using O(1) data structures.

Refactored from a monolithic `telbot.py`/`bota.py`, inspired by
[jackwener/tg-cli](https://github.com/jackwener/tg-cli) (local-first, Click CLI, structured
output, externalized rate-limiting).

> 📈 **Architecture diagram:** see [docs/architecture.md](docs/architecture.md) (Mermaid).

---

## ⚠️ Security: no secrets in git

This repo **never** commits credentials. `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, session
files, and `*.db` are gitignored. You put them in a local `.env` file that is **never
pushed**. Always confirm with `git status --short | grep '.env'` before committing.

---

## Install

Requires [uv](https://docs.astral.sh/uv/) (same mechanism as `tg-cli`). Two one-line
global installs:

**uv (recommended, no clone needed):**

```bash
uv tool install "git+https://github.com/dbillion/tgforwarder.git"
# -> installs the global `tgf` command (~/.local/bin/tgf)
```

**From a clone:**

```bash
git clone https://github.com/dbillion/tgforwarder.git && cd tgforwarder
uv tool install . --no-cache
which tgf                          # -> ~/.local/bin/tgf
```

**npx (agent-friendly wrapper):** `npx -y tgf-forwarder` installs `tgf` via the uv
one-liner above (requires `uv` on PATH). See `installer/package.json`.

For local development:

```bash
uv venv .venv && . .venv/bin/activate
uv pip install -e .
uv pip install kreuzberg           # fast Rust OCR (optional but recommended)
```

Verify:

```bash
tgf --help
tgf status                        # "api configured: yes" when .env is present
```

---

## Configure

Create `tgforwarder/.env` (gitignored — do **not** commit it):

```dotenv
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TG_SESSION_NAME=forwarder_session1
SOURCE_CHANNELS=-1000000000000        # source channel(s), comma-separated
DEST_CHANNELS=-1000000000001,-1000000000002   # destination(s), comma-separated
FORWARD_PATH=downloads               # local download dir for the OCR fallback
```

You need a **logged-in user session** (MTProto cannot use a bot token for this). Copy your
existing `.session` file to `~/.local/share/tg-cli/forwarder_session1.session`, or generate
one with a Telethon login helper. One session at a time (a concurrent sync holds the DB
lock).

> **Tip:** to forward to your **Saved Messages**, use your own user ID as the destination,
> e.g. `--dest <YOUR_USER_ID>` (your Saved Messages user id from `tgf score`/`get_me`).

---

## Usage

```bash
# Interactive (prompts source/dest/order/mode):
tgf forward

# Explicit, oldest-first (default), 10 messages:
tgf forward --source <SOURCE_CHANNEL> --dest <YOUR_USER_ID> --limit 10

# Forward EVERYTHING, chronological from the channel start:
tgf forward --source <SOURCE_CHANNEL> --dest <YOUR_USER_ID> --all --delay 1 --batch 25

# Resume a previous run (continues from saved last-message id):
tgf forward --resume

# Newest-first:
tgf forward --order newest --limit 50

# Chat usefulness scoring (needs tg-cli's messages.db):
tgf score --db ~/.local/share/tg-cli/messages.db --topic "rust,devops,ai" --top 5

# OCR-only check (read-only, needs a numeric channel ID):
tgf test-ocr --source <SOURCE_CHANNEL>
```

### Options (`tgf forward --help`)

| Option | Meaning |
|---|---|
| `--source` | Source channel/user ID or `@handle` (or `SOURCE_CHANNELS` in `.env`) |
| `--dest` | Destination (repeatable; or `DEST_CHANNELS`) — Saved Messages = your user ID |
| `--path` | Local download dir for the OCR fallback (default `./downloads`) |
| `--order` | `oldest` (default) or `newest` |
| `--all` | Process the entire channel |
| `--limit N` | Cap messages (ignored with `--all`) |
| `--resume` | Continue from last forwarded message id |
| `--start` | Start from the beginning (ignore saved progress) |
| `--delay S` | Seconds between **batches** (anti-ban; default 1.0) |
| `--batch N` | Messages per forward API call — one call moves the whole batch (default 25). Raise for speed, lower if rate-limited |

#### Throughput

`tgf` batches `N` messages into a **single** `forward_messages` API call, so throughput is
`N` files per call. With `--batch 25 --delay 1` you move ~25 files/second of wall-clock
(one call + 1s pause). Telegram's copy endpoint can sustain far more; if you're not
rate-limited, raise `--batch` (e.g. 50–100) and drop `--delay` toward 0. The dedup cache
and O(1) `set` lookup keep per-message overhead flat at 5000+ files.

### Deleted-account chats

If a chat belongs to a **deleted account** you can still read, `resolve_entity` falls back
to the session's cached `InputPeer`, so forwarding still works (native `forward_messages`
preserves the original file exactly).

---

## Upgrade

```bash
cd tgforwarder
git pull                 # or copy updated sources
uv tool uninstall tgforwarder
uv build --wheel --no-cache
uv tool install . --no-cache
```

Add a dependency (e.g. a new OCR backend):

```bash
uv pip install some-pkg
# then add it to dependencies in pyproject.toml and rebuild
```

Run the test suite (offline, no network/API):

```bash
uv run pytest -q        # 14 tests
```

---

## For agents (npx-style install)

Agents can install and operate `tgf` without manual setup. Load the
**`tgf-agent-install`** skill (Hermes) which runs the `uv tool install` flow and a
verification checklist. Minimum the agent must do:

```bash
# 1. ensure uv
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. install tgf from this repo
uv tool install . --no-cache
# 3. write a local .env (NEVER commit it) with API id/hash + session
# 4. verify
tgf status
```

> Agents should treat `.env` as a secret: write it locally, gitignore it, and never paste
> values into shared logs.

---

## Project layout

```
tgforwarder/
├── pyproject.toml          # setuptools build, [project.scripts] tgf
├── tgforwarder/
│   ├── client.py           # TelegramClient + resolve_entity (+ deleted-account fallback)
│   ├── cache.py            # ForwardCache (SQLite dedup, load_done_set, mark_many)
│   ├── forward.py          # kreuzberg (Rust) OCR primary, Tesseract fallback
│   ├── state.py            # resume persistence (last_id + direction)
│   ├── report.py           # ForwardLogger (deque/Counter, O(1) at 5000+ files)
│   ├── cli.py              # Click CLI
│   └── __init__.py
├── tests/test_offline.py   # 14 offline tests
├── docs/architecture.md    # Mermaid diagram + data-flow
└── README.md
```

## License

MIT.
