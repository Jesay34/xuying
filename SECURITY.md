# Security Policy

## Supported version

This repository is currently in alpha. Security fixes are applied to the latest alpha release.

## Important deployment boundary

Xuying's Web UI is intended for a trusted LAN. It does not currently provide a public-Internet authentication layer. Do not expose port 3434 directly to the Internet. If remote access is required, place Xuying behind a VPN or a reverse proxy with authentication and TLS.

## Secrets handled by Xuying

A normal installation may contain:

- Telegram API ID / API Hash
- Telegram phone number
- Telegram account Session files
- Telegram Bot token
- Immich API key
- Telegram channel/user IDs and local media paths

Runtime secrets are stored under `/config` and are excluded by the repository's `.gitignore` and `.dockerignore`. Treat Telegram Session files as credentials.

## Reporting a vulnerability

Please avoid opening a public Issue containing credentials, private channel IDs, private media paths or reproducible account data. Redact all sensitive values before sharing logs or screenshots.

If GitHub Private Vulnerability Reporting is available for the repository, use it for security-sensitive reports. Otherwise, open only a minimal redacted Issue describing the affected component without exploit details, credentials, account identifiers or private paths.
