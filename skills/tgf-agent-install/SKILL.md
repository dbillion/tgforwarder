---
name: tgf-agent-install
description: Install/operate the tgf Telegram forwarder CLI for agents.
---

# tgf-agent-install — install + operate `tgf` for agents

Use when an agent (or user via an agent) needs to **install, configure, and run** the
`tgf` Telegram forwarder. `tgf` forwards media/text from a Telegram channel or
deleted-account chat to Saved Messages or other channels, using native MTProto
forwarding (+ optional Rust/kreuzberg OCR). Full design: see `tgf-telegram-forwarder-setup`.

## One-line installs (no clone needed)

```bash
# uv, pinned to a release tag (recommended — reproducible):
uv tool install "git+https://github.com/dbillion/tgforwarder.git@v0.1.0"

# or run without installing (uvx), good for CI/one-off:
uvx --from "git+https://github.com/dbillion/tgforwarder.git@v0.1.0" tgf --help

# agent skill install (no npm publish needed):
npx skills --skill dbillion/tgforwarder/tgf-agent-install
```

## Agent setup flow

1. Ensure `uv` is present:
   ```bash
   command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
2. Install `tgf` (see one-liners above) → `tgf` on PATH (`~/.local/bin/tgf`).
3. **Write `.env` locally — NEVER commit it. Secrets only.** Required keys:
   ```
   TELEGRAM_API_ID=<id>
   TELEGRAM_API_HASH=<hash>
   TG_SESSION_NAME=forwarder_session1
   SOURCE_CHANNELS=-100...
   DEST_CHANNELS=-100...,<YOUR_USER_ID>      # <YOUR_USER_ID> = your Saved Messages id
   FORWARD_PATH=downloads
   ```
   Use a heredoc or `write_file` to `~/<repo>/.env`, then confirm gitignored:
   `git status --short | grep '.env'` must show NOTHING.
4. A logged-in **user** `.session` must exist at
   `~/.local/share/tg-cli/forwarder_session1.session` (MTProto can't use a bot token).
5. Verify: `tgf status` → "api configured: yes".

## Run (for the user)

```bash
tgf forward --source <CHANNEL> --dest <YOUR_USER_ID> --order oldest --all --batch 25 --delay 1
# or interactive:
tgf forward
```

- `--dest <YOUR_USER_ID>` = the user's **Saved Messages** (get it from `tgf score`/`get_me`).
- Deleted-account chats: pass the raw numeric id; `resolve_entity` falls back to the
  cached dialog `InputPeer` so it still forwards.
- Batched throughput: `--batch 25` moves 25 files per API call; `--delay 1` = 1s between
  batches → ~25 files/sec. Raise `--batch` (50–100) / drop `--delay` if not rate-limited.
- Resume: `tgf forward --resume` continues from `.forward_state.json`.

## Pitfalls (agent must avoid)
- **Never print API id/hash/session** in any output or log.
- **Hatchling wheels break** (`uv tool install` installs empty package). Use `setuptools`
  in `pyproject.toml` (already set).
- `find_dotenv()` walks UP and grabs a parent `.env` — `load_dotenv(Path(".env"))` is used.
  Run `tgf` from the project dir.
- Native `forward_messages` is the primary path (download+OCR is fallback). Verify delivery
  by re-reading the returned message id from the target, not by trusting log lines.
- One Telegram session at a time (concurrent sync holds the DB lock).

## Verify before claiming success
1. `tgf status` → "api configured: yes"
2. `tgf forward --limit 2 --start` → returns message ids; re-read one from the destination
   to confirm it actually arrived (don't trust "forwarded: N" from cache alone).
3. `git status` shows `.env` UNTRACKED.
