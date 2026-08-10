# Contributing to Xuying

Thanks for helping improve Xuying.

## Before opening an Issue

- Reproduce the problem on the latest release when practical.
- Remove Telegram API credentials, phone numbers, Session files, Bot tokens, Immich API keys, private channel IDs and private media paths.
- Include the Xuying version, Docker/Compose version, NAS/OS type and the smallest useful log excerpt.

## Pull requests

1. Create a focused branch and keep unrelated refactors out of the PR.
2. Preserve the core safety rule: original media in `raw/` is not moved or overwritten by the organizer.
3. Keep `raw/` and `library/` hard-link semantics intact unless the change explicitly targets storage design.
4. Run:

```bash
python -m compileall -q app
python scripts/check_public_release.py
```

5. Explain migration impact for changes to database schema, paths, filenames, Telegram grouping or Immich album behavior.

## Style

Prefer small, reviewable changes. User-facing text can be Chinese; code identifiers and public technical documentation should remain clear and consistent.
