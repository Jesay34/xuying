# Changelog

All notable changes to Xuying are documented here.

## 1.0.0-alpha.59

### Public-release preparation

- Sanitized all public defaults and examples so the repository ships without deployment-specific channel IDs, LAN addresses or host paths.
- Removed deployment-specific compatibility behavior from the public source tree.
- Added portable Docker Compose host paths via `.env`.
- Switched public Docker defaults to the official Python image and PyPI, while retaining optional mirror configuration.
- Added `.gitignore`, security guidance, contribution guidance, architecture notes, roadmap and CI checks.
- Aligned public version metadata to `1.0.0-alpha.59`.

### Existing alpha functionality

- Telegram live monitoring and recoverable history completion.
- Hard-link based person/batch library organization and XMP sidecars.
- Independent Bot forwarded-media library.
- Immich external-library scanning and album membership repair.
- Broader Immich media-extension handling and MIME-based extension recovery for Telegram documents without filenames.
- Live Photo-aware Immich album verification.
