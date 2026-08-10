from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def configured_timezone(name: str) -> tzinfo:
    """Resolve the configured local timezone with a NAS-safe fallback."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        return timezone.utc


def local_datetime(value: datetime, timezone_name: str) -> datetime:
    """Convert Telegram's UTC timestamp to the configured local wall clock."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(configured_timezone(timezone_name))


def local_month(value: datetime, timezone_name: str) -> str:
    return local_datetime(value, timezone_name).strftime("%Y_%m")
