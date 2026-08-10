from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

IGNORED_DIRS = {".git", "__pycache__", ".venv", "data"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp"}

FORBIDDEN_NAMES = {
    "config.yaml",
    "secrets.env",
    ".env",
}

SUSPICIOUS_PATTERNS = {
    "Telegram Bot token": re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "OpenAI-style secret key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "hard-coded Telegram channel ID": re.compile(r"(?<!\d)-100\d{8,16}(?!\d)"),
    "host-specific NAS volume path": re.compile(r"/(?:volume|vol)\d+/[^\s\"'`]*"),
    # Build this pattern in pieces so the release checker does not match its own source.
    "macOS user home path": re.compile(r"/" + r"Users/[^/\s]+/"),
    "Windows user home path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"),
    "non-empty Telegram API hash assignment": re.compile(
        r"(?im)^\s*(?:XUYING_TELEGRAM_API_HASH|telegram_api_hash|api_hash)\s*[:=]\s*[\"']?[A-Fa-f0-9]{24,}[\"']?\s*$"
    ),
    "non-empty Immich API key assignment": re.compile(
        r"(?im)^\s*(?:XUYING_IMMICH_API_KEY|immich_api_key|api_key)\s*[:=]\s*[\"']?[A-Za-z0-9._-]{20,}[\"']?\s*$"
    ),
}

EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IPV4_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")

# Deliberately fake documentation values that may appear in UI placeholders.
ALLOWED_EXAMPLE_PHONES = {"+8613800000000", "8613800000000", "13800000000"}
ALLOWED_IPS = {"0.0.0.0", "127.0.0.1"}


def iter_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        yield path


def iter_text_files():
    for path in iter_files():
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        yield path


def main() -> int:
    problems: list[str] = []

    for path in iter_files():
        rel = path.relative_to(ROOT)
        if path.name in FORBIDDEN_NAMES:
            problems.append(f"forbidden runtime/environment file: {rel}")
        if path.suffix == ".session" or ".session-" in path.name:
            problems.append(f"Telegram session file: {rel}")
        if path.suffix in {".db", ".sqlite", ".sqlite3"}:
            problems.append(f"runtime database: {rel}")

    for path in iter_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT)

        for label, pattern in SUSPICIOUS_PATTERNS.items():
            if pattern.search(text):
                problems.append(f"{label} in {rel}")

        for match in EMAIL_PATTERN.finditer(text):
            problems.append(f"email address in {rel}")
            break

        for match in PHONE_PATTERN.finditer(text):
            if match.group(0) not in ALLOWED_EXAMPLE_PHONES:
                problems.append(f"phone-number-like value in {rel}")
                break

        for match in IPV4_PATTERN.finditer(text):
            value = match.group(0)
            if value in ALLOWED_IPS:
                continue
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.is_private:
                problems.append(f"private IPv4 address in {rel}")
                break

    if problems:
        print("Public release check FAILED:")
        for problem in sorted(set(problems)):
            print(f" - {problem}")
        return 1

    print("Public release check passed: no runtime secret files or common deployment identifiers found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
