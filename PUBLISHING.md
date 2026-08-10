# GitHub publishing checklist

This source tree is prepared as a public repository, but do one final check before the first push.

## Recommended repository metadata

**Repository name**

```text
xuying
```

**Short description**

```text
Self-hosted Telegram media archiver and organizer with hard-link libraries, XMP metadata and Immich album sync.
```

**Suggested topics**

```text
telegram
telegram-downloader
immich
nas
self-hosted
docker
media-management
photo-management
fastapi
telethon
xmp
```

## Before the first push

1. Run `python scripts/check_public_release.py`.
2. Confirm there is no `config.yaml`, `secrets.env`, `.env`, database, Telegram Session or real media under the repository directory.
3. Search screenshots before uploading them; UI screenshots can expose phone numbers, channel names/IDs, local IP addresses, filenames and Immich details.
4. Keep the repository public only after the first commit has been reviewed. Deleting a secret in a later commit is not sufficient because Git history can retain it.
5. Turn on GitHub's available secret-scanning/security options for the repository.

## Suggested first release

Tag:

```text
v1.0.0-alpha.59
```

Release title:

```text
Xuying 1.0.0-alpha.59 — first public alpha
```

Release notes should focus on the public feature set and known alpha limitations rather than private deployment history.
