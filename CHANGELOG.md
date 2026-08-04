# Changelog

## v0.2.0

### Bug fixes
- Channel-sourced forward dedup now matches correctly: forwards from a source
  channel are identified via the properly-typed peer (`PeerChannel`), not a
  hardcoded `PeerUser`. (Was silently skipping dedup for channel sources.)
- `.env` is now loaded relative to the package, not the current working
  directory — credentials resolve correctly when the CLI is run from any folder.
- Interactive credential prompt: `tgf` now prompts for `TELEGRAM_API_ID` /
  `TELEGRAM_API_HASH` (and persists them to the gitignored `.env`) instead of
  hard-failing with a cryptic message when they are missing.

### Features
- `tgf login` command: dedicated one-time Telegram session auth. Reads phone /
  2FA password via prompt and gives a clear error when API credentials are
  invalid (`ApiIdInvalidError`) or Telegram is rate-limiting login
  (`FloodWaitError`).
- `tgf status` now explains what to do when the API is not configured.

### Engineering
- Modularized the 694-line `cli.py` monolith into `peer.py` (primitives),
  `commands.py` (command bodies), `login.py`, `dedupe.py`, `copy_mode.py` — all
  under ~225 lines, matching the repo's one-concern-per-module style.
- Split the god `test_offline.py` (353 lines) into per-module `test_*.py` files.
- Added `tests/test_copy_mode.py` and `tests/test_forward_run.py` (stubbed
  integration test of the full forward pipeline, no network).
- Secret hygiene: `.gitignore` hardened so `.env` / `*.session` never upload while
  `.env.example` commits; added `scripts/secret-scan.sh` with a pre-commit hook
  (`.githooks/pre-commit`) and a GitHub Action (`.github/workflows/secret-scan.yml`).

## v0.1.0
- Initial release.
