# Privacy notes

Xuying is self-hosted. The application stores its runtime state on the paths mounted into `/config` and `/media/xydown`.

## Sensitive local data

A normal installation can contain Telegram account sessions, API credentials, a phone number, Bot tokens, channel/user IDs, Immich API keys, local media paths, filenames, and a SQLite database describing downloaded media. Treat the entire runtime `/config` directory as private.

## Network behavior

Xuying does not include analytics or telemetry code. It communicates with services required for the features you configure, including Telegram, Immich, and an optional HTTP/SOCKS proxy.

## Public bug reports

Before posting logs, screenshots, configuration fragments, database rows, or file trees, redact:

- API IDs, API hashes, API keys, tokens, passwords, and session data
- phone numbers and email addresses
- Telegram channel/user IDs and invite links
- private IP addresses, hostnames, domains, NAS mount paths, and usernames
- filenames or media metadata that may identify people or locations

The repository includes `scripts/check_public_release.py` as a last-line hygiene check for source releases. It is not a substitute for reviewing screenshots and logs manually.
