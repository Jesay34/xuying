# Architecture

Xuying separates acquisition, persistence, organization and presentation so that media can be repaired or re-indexed without redownloading everything.

## Data flow

```text
Telegram
   │
   ├─ live monitor / history completion / Bot forwarding
   ▼
raw media  ─────► SQLite metadata
   │
   ├─ grouping + ordering
   ▼
hard-link library + XMP
   │
   ├─ external library scan
   ▼
Immich
   └─ album membership reconciliation
```

## Main components

- `TelegramService`: authentication, live events, history downloads, Bot tasks and media persistence.
- `HistoryRebuildService`: recoverable completion tasks, migrations and safe reconciliation.
- `organizer.py`: creates the organized hard-link view without moving original media.
- `ImmichClient`: wraps the Immich HTTP API.
- `ImmichSyncOrchestrator`: coordinates external-library scans and album correction.
- SQLite: records Telegram messages, media files, groups and task state.

## Storage invariant

The organizer treats the original downloaded file as the source of truth. A normal organization operation creates hard links and sidecar metadata; it does not move or overwrite the original raw media.

Because POSIX hard links cannot cross filesystems, `raw/` and `library/` must live under the same mounted filesystem.
