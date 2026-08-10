## What changed

Describe the change and why it is needed.

## Safety / data impact

- [ ] Original media in `raw/` is not moved or overwritten.
- [ ] Hard-link behavior is preserved, or the storage change is explicitly documented.
- [ ] I did not include credentials, account identifiers, private paths, screenshots with personal data, runtime databases, or Telegram session files.

## Checks

- [ ] `python -m compileall -q app`
- [ ] `python scripts/check_public_release.py`
- [ ] Docker build tested when the change affects packaging or dependencies.
