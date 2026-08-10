# Xuying

> A self-hosted NAS service for Telegram media archiving, deterministic organization, XMP sidecars, and Immich album synchronization.

Current version: **1.0.0-alpha.60** · [中文 README](README.md)

Xuying is designed for media that you are authorized to access and store. It keeps original downloads in `raw/`, builds an organized library using hard links, and lets Immich index the organized view without moving or duplicating the source media.

## Highlights

- Telegram user-account login, live channel monitoring, and resumable history backfill
- Optional Telegram Bot forwarded-media workflow isolated from channel archives
- `Chat ID + Message ID` mutual exclusion to prevent duplicate writes between live monitoring and history backfill
- Telegram album grouping or marker-based batch boundaries
- Original media preserved under `raw/`; organized libraries use hard links
- Stable ordering using message order, filename hints, and camera sequence metadata
- XMP sidecar generation
- Immich external-library scanning, album creation, and idempotent membership repair
- Live Photo-aware Immich album synchronization
- Web management UI with pause/resume and recoverable history jobs

## Quick start

```bash
git clone https://github.com/Jesay34/xuying.git
cd xuying
cp .env.example .env
docker compose up -d --build
```

Open `http://<NAS-IP>:3434` in a browser. On first run, enter your own Telegram API credentials and complete account login in the Web UI.

By default, persistent data is stored under `./data/`. On a NAS, set `XUYING_MEDIA_DIR` and `XUYING_CONFIG_DIR` in `.env` to host paths of your choice.

**Important:** `raw/` and `library/` must reside on the same filesystem/mount for hard links to work.

## Storage layout

```text
/media/xydown/
├── raw/
├── library/
├── rebuild/
└── forwarded/
    ├── raw/
    └── library/

/config/
├── config.yaml
├── secrets.env
├── xuying.db
└── sessions/
```

Runtime configuration, credentials, databases, Telegram sessions, and media are excluded from Git and the Docker build context. Never publish those files in Issues, screenshots, or logs.

## Immich

Mount only the organized library paths into Immich external libraries. Do not index both `raw/` and the hard-link library unless duplicate-looking assets are intentional.

Example Immich mounts:

```yaml
- /path/to/xuying-media/library:/external/xuying-main:ro
- /path/to/xuying-media/forwarded/library:/external/xuying-forwarded:ro
```

Then configure the matching container-side paths in Xuying's Immich settings.

## Security and privacy

Xuying's Web UI currently has no Internet-facing authentication layer. Keep port `3434` on a trusted LAN, or place it behind your own authenticated VPN/reverse proxy. Telegram session files must be treated as credentials.

Xuying does not include analytics or telemetry code. Network access is used for the services you configure, such as Telegram, Immich, and an optional proxy.

See [SECURITY.md](SECURITY.md) and [docs/PRIVACY.md](docs/PRIVACY.md).

## Development checks

```bash
python -m compileall -q app
python scripts/check_public_release.py
```

GitHub Actions runs the same public-release hygiene checks and also validates the Docker build.

## Project status

Xuying is alpha software. Reliability, transparent storage behavior, and preservation of original media take priority over rapid feature expansion. See [ROADMAP.md](ROADMAP.md).

## Contributing

Issues and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first, and always redact credentials, channel IDs, local paths, phone numbers, and account data from reports.

## Upstream lineage

The Telegram download implementation evolved from the open-source `hermes-telegram-downloader` / `telegram_media_downloader` lineage. Required MIT notices are preserved in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Responsible use

Only archive media you are authorized to access and store. Follow applicable law, Telegram's terms, and relevant copyright/privacy obligations.

## License

[MIT License](LICENSE)
