#!/bin/sh
# Secret scanner engine for tgforwarder.
# Modes:
#   staged  -> scan only currently-staged changes (used by the pre-commit hook)
#   all     -> scan every tracked file in the tree (used by CI)
#
# Exits non-zero (blocks) if a likely secret is found. Skippable with
# SECRET_SCAN_SKIP=1 (or `git commit --no-verify` for the hook only).
#
# Design notes:
#  - .env / *.session are blocked by filename too (in case someone force-adds).
#  - .env.example is skipped entirely (placeholders only).
#  - Known non-secret fixtures (test deadbeef placeholder, YOUR_*_HERE) are allowlisted
#    so normal development/commits are not blocked by false positives.
#  - The 32-hex rule uses surrounding-char boundaries so it does NOT match substrings
#    inside longer hashes (e.g. sha256 content hashes in code).

set -eu

MODE="${1:-staged}"
if [ "${SECRET_SCAN_SKIP:-0}" = "1" ]; then
  echo "secret-scan: skipped (SECRET_SCAN_SKIP=1)"
  exit 0
fi

# Tokens that are NOT real secrets — never block on these.
ALLOW='deadbeefdeadbeefdeadbeefdeadbeef|YOUR_API_HASH_HERE|YOUR_API_ID_HERE|api_hash_here|api_id_here'

# Forbidden patterns (POSIX ERE). The 32-hex rule is bounded so it won't fire on
# substrings of longer hex runs (sha256 etc.).
PATTERNS='-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|xox[baprs]-[0-9A-Za-z-]{10,}|(^|[^0-9a-fA-F])[0-9a-f]{32}([^0-9a-fA-F]|$)'

if [ "$MODE" = "staged" ]; then
  FILES=$(git diff --cached --name-only --diff-filter=ACM)
  GIT_GREP_TARGET="--cached"
else
  FILES=$(git ls-files)
  GIT_GREP_TARGET="HEAD"
fi

if [ -z "$FILES" ]; then
  echo "secret-scan: nothing to scan ($MODE)"
  exit 0
fi

# Block secret-bearing filenames outright (force-add guard).
BAD_NAME=0
for f in $FILES; do
  case "$f" in
    .env|.env.*|*.session|*.session-journal)
      # .env.example is the committed placeholder template — never block it.
      [ "$f" = ".env.example" ] && continue
      echo "secret-scan: BLOCKED filename: $f (never commit real secrets; use .env.example)"
      BAD_NAME=1 ;;
  esac
done
if [ "$BAD_NAME" = "1" ]; then
  exit 1
fi

# Build the scan list, skipping .env.example (placeholders only).
SCAN_FILES=""
for f in $FILES; do
  case "$f" in
    *.env.example) ;;
    *) SCAN_FILES="$SCAN_FILES $f" ;;
  esac
done

if [ -z "$SCAN_FILES" ]; then
  echo "secret-scan: clean ($MODE)"
  exit 0
fi

# shellcheck disable=SC2086
MATCHES=$(git grep -nE -e "$PATTERNS" $GIT_GREP_TARGET -- $SCAN_FILES 2>/dev/null | grep -vE "$ALLOW" || true)

if [ -n "$MATCHES" ]; then
  echo "secret-scan: POSSIBLE SECRET DETECTED ($MODE):"
  echo "$MATCHES"
  echo ""
  echo "If this is a false positive, add the token to the ALLOW list in scripts/secret-scan.sh,"
  echo "or bypass locally with: git commit --no-verify   (or SECRET_SCAN_SKIP=1)."
  exit 1
fi

echo "secret-scan: clean ($MODE)"
exit 0
