# tgforwarder — How It Works

```mermaid
flowchart TD
    A[User runs tgf forward] --> B{Args or .env?}
    B -->|no args| C[Interactive menu:<br/>source / dest / order / mode]
    B -->|--source/--dest or .env| D[Resolve config]
    C --> D
    D --> E[resolve_entity<br/>numeric ID / @handle / name]
    E -->|deleted-account chat| E1[Cached-dialog<br/>InputPeer fallback]
    E --> F[TelegramClient<br/>MTProto session]
    F --> G[iter_messages<br/>order=oldest|newest, --all, resume]
    G --> H{For each message}
    H --> I{Already in done set?<br/>O(1) set lookup}
    I -->|yes| H
    I -->|no| J[forward_messages<br/>native Telegram copy]
    J -->|fails| K[fallback: download_media<br/>+ kreuzberg OCR rename<br/>+ send_file]
    K --> L[mark cache + logger.record]
    J --> L
    L --> M{50 buffered?}
    M -->|yes| N[mark_many<br/>executemany batch]
    M -->|no| H
    N --> H
    H -->|done| O[Persist resume state<br/>last_id + direction]
    O --> P[ForwardLogger.render<br/>count / types / 5-min window / names]
    P --> Q[(Saved Messages / target channels)]

    R[(.env: API id/hash<br/>SOURCE/DEST/PATH)] -.creds.-> F
    S[(forwarder.db SQLite<br/>dedup cache)] -.load_done_set.-> I
    T[(.forward_state.json<br/>resume)] -.load/save.-> G
    U[(kreuzberg: Rust OCR<br/>Python API)] -.fallback OCR.-> K
```

## Data-flow summary

1. **Config** — source/dest/path come from CLI flags or `.env` (`SOURCE_CHANNELS`,
   `DEST_CHANNELS`, `FORWARD_PATH`). No secrets are committed; `.env` is gitignored.
2. **Resolution** — `resolve_entity` handles numeric IDs (with `-100` prefix), `@handles`,
   and names. For **deleted-account chats** it falls back to a cached dialog `InputPeer`
   (the server won't return a deleted user by ID, but the session keeps the peer).
3. **Iteration** — `iter_messages` walks the source. `order=oldest` (default) uses
   `reverse=True`; `min_id` + resume state skip already-processed messages.
4. **Dedup (O(1))** — `ForwardCache.load_done_set()` bulk-loads processed `msg_id`s into a
   Python `set`; the loop checks `msg.id in done` instead of per-row SQL.
5. **Forward** — native `forward_messages` copies the exact file instantly (works for
   deleted accounts). If that fails, it falls back to download → **kreuzberg** (Rust) OCR
   rename → re-upload.
6. **Persistence** — marks are flushed in batches of 50 (`mark_many`); resume state
   (`last_message_id` + `direction`) is saved to `.forward_state.json`.
7. **Reporting** — `ForwardLogger` uses a bounded `deque` + `Counter` + window `deque`
   (all O(1)) so it stays fast even at 5000+ files, rendering an emoji summary.

## Module map

| Module | Responsibility |
|---|---|
| `client.py` | TelegramClient session + `resolve_entity` (+ deleted-account fallback) |
| `cache.py` | `ForwardCache` SQLite dedup; `load_done_set`, `mark_many` |
| `forward.py` | OCR via **kreuzberg** (Rust) primary, Tesseract fallback |
| `state.py` | resume persistence: `last_message_id` + `direction` |
| `report.py` | `ForwardLogger`: deque/Counter-based O(1) reporting |
| `cli.py` | Click CLI: `forward` / `score` / `test-ocr` / `status` |
| `tests/test_offline.py` | 14 offline tests (no network/API) |
