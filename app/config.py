from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class AppConfig(BaseModel):
    name: str = "序影 Xuying"
    host: str = "0.0.0.0"
    port: int = 3434
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"


class StorageConfig(BaseModel):
    media_root: Path = Path("/media/xydown")
    download_path: Path = Path("/media/xydown/raw")
    library_path: Path = Path("/media/xydown/library")
    rebuild_path: Path = Path("/media/xydown/rebuild")
    forwarded_path: Path = Path("/media/xydown/forwarded/raw")
    forwarded_library_path: Path = Path("/media/xydown/forwarded/library")
    data_path: Path = Path("/config")
    session_path: Path = Path("/config/sessions")

    @model_validator(mode="after")
    def distinct_roots(self) -> "StorageConfig":
        default_root = Path("/media/xydown")
        if self.media_root != default_root:
            if self.forwarded_path == default_root / "forwarded" / "raw":
                self.forwarded_path = self.media_root / "forwarded" / "raw"
            if self.forwarded_library_path == default_root / "forwarded" / "library":
                self.forwarded_library_path = self.media_root / "forwarded" / "library"
        roots = {self.download_path, self.library_path, self.rebuild_path, self.forwarded_path, self.forwarded_library_path}
        if len(roots) != 5:
            raise ValueError("raw, library, rebuild and forwarded paths must be distinct")
        return self


class ChannelConfig(BaseModel):
    name: str
    chat_id: int
    enabled: bool = True
    start_message_id: int = 0
    start_mode: str = "message_id"
    start_date: date | None = None
    grouping_mode: str = "telegram_album"
    marker_text: str = "1"
    advertisement_policy: str = "quarantine"
    advertisement_keywords: list[str] = Field(
        default_factory=lambda: [
            "广告",
            "推广",
            "加入群",
            "加入内部群",
            "进群",
            "永久会员",
            "一次付费",
            "自助下单",
            "售后",
            "优惠",
            "价格",
            "t.me/",
        ]
    )
    display_spacing_hours: int = 24
    timeline_mode: str = "album"
    display_order: int = 0
    max_concurrent_downloads: int = 5

    @model_validator(mode="after")
    def validate_rules(self) -> "ChannelConfig":
        if self.start_mode not in {"now", "date", "message_id"}:
            raise ValueError("start_mode 只能是 now、date 或 message_id")
        if self.start_mode == "date" and self.start_date is None:
            raise ValueError("按日期开始监听时必须选择日期")
        if self.grouping_mode not in {"telegram_album", "marker"}:
            raise ValueError("grouping_mode 只能是 telegram_album 或 marker")
        if self.advertisement_policy not in {"quarantine", "keep"}:
            raise ValueError("advertisement_policy 只能是 quarantine 或 keep")
        if self.timeline_mode not in {"album", "spaced"}:
            raise ValueError("timeline_mode 只能是 album 或 spaced")
        if not 1 <= self.display_spacing_hours <= 168:
            raise ValueError("display_spacing_hours 必须在 1 到 168 小时之间")
        if not 0 <= self.display_order <= 9999:
            raise ValueError("display_order 必须在 0 到 9999 之间")
        if not 1 <= self.max_concurrent_downloads <= 8:
            raise ValueError("max_concurrent_downloads 必须在 1 到 8 之间")
        return self


class TelegramConfig(BaseModel):
    enabled: bool = False
    session_name: str = "xuying"
    proxy_url: str = ""
    channels: list[ChannelConfig] = Field(default_factory=list)
    bot_enabled: bool = False
    bot_allowed_user_ids: list[int] = Field(default_factory=list)
    bot_max_concurrent_downloads: int = Field(default=5, ge=1, le=8)


class OrganizerConfig(BaseModel):
    enabled: bool = True
    auto_organize: bool = True
    link_mode: str = "hardlink"
    group_by: str = "telegram_media_group"
    preserve_original_order: bool = True
    camera_sequence_sort: bool = True
    generate_xmp: bool = True
    group_gap_seconds: int = 30

    @model_validator(mode="after")
    def hardlink_only(self) -> "OrganizerConfig":
        if self.link_mode != "hardlink":
            raise ValueError("当前安全版本只允许 hardlink，不会复制或移动原文件")
        return self


class ImmichConfig(BaseModel):
    enabled: bool = False
    base_url: str = "http://immich-server:2283"
    library_id: str = ""
    external_library_path: str = "/external/xuying-main"
    auto_scan: bool = False
    auto_album: bool = False
    album_prefix: str = "序影"
    forwarded_library_id: str = ""
    forwarded_external_library_path: str = "/external/xuying-forwarded"
    forwarded_auto_scan: bool = True
    forwarded_auto_album: bool = True
    forwarded_auto_archive: bool = True
    forwarded_album_prefix: str = "序影 · 机器人"


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    organizer: OrganizerConfig = Field(default_factory=OrganizerConfig)
    immich: ImmichConfig = Field(default_factory=ImmichConfig)

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.storage.data_path / 'xuying.db'}"

    @property
    def telegram_api_id(self) -> int | None:
        raw = os.getenv("XUYING_TELEGRAM_API_ID", "").strip()
        return int(raw) if raw else None

    @property
    def telegram_api_hash(self) -> str | None:
        return os.getenv("XUYING_TELEGRAM_API_HASH") or None

    @property
    def immich_api_key(self) -> str | None:
        return os.getenv("XUYING_IMMICH_API_KEY") or None

    @property
    def telegram_bot_token(self) -> str | None:
        return os.getenv("XUYING_TELEGRAM_BOT_TOKEN") or None

    def ensure_directories(self) -> None:
        self.storage.data_path.mkdir(parents=True, exist_ok=True)
        self.storage.session_path.mkdir(parents=True, exist_ok=True)
        self.storage.media_root.mkdir(parents=True, exist_ok=True)
        self.storage.download_path.mkdir(parents=True, exist_ok=True)
        self.storage.library_path.mkdir(parents=True, exist_ok=True)
        self.storage.rebuild_path.mkdir(parents=True, exist_ok=True)
        self.storage.forwarded_path.mkdir(parents=True, exist_ok=True)
        self.storage.forwarded_library_path.mkdir(parents=True, exist_ok=True)


def _default_config_source() -> Path:
    return Path(__file__).resolve().parent.parent / "config.example.yaml"


def runtime_config_path(path: str | Path | None = None) -> Path:
    return Path(path or os.getenv("XUYING_CONFIG", "/config/config.yaml"))


def runtime_secrets_path(path: str | Path | None = None) -> Path:
    return runtime_config_path(path).parent / "secrets.env"


def load_runtime_secrets(path: str | Path | None = None) -> None:
    secrets_path = runtime_secrets_path(path)
    if not secrets_path.exists():
        return
    for raw_line in secrets_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and not os.getenv(key):
            os.environ[key] = value


def save_runtime_secrets(
    api_id: int,
    api_hash: str,
    phone: str,
    path: str | Path | None = None,
) -> Path:
    secrets_path = runtime_secrets_path(path)
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    values = _read_secret_values(secrets_path)
    values.update(
        {
            "XUYING_TELEGRAM_API_ID": str(api_id),
            "XUYING_TELEGRAM_API_HASH": api_hash.strip(),
            "XUYING_TELEGRAM_PHONE": phone.strip(),
        }
    )
    content = _serialize_secret_values(values)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".secrets-", suffix=".tmp", dir=secrets_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, secrets_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    os.environ["XUYING_TELEGRAM_API_ID"] = str(api_id)
    os.environ["XUYING_TELEGRAM_API_HASH"] = api_hash.strip()
    os.environ["XUYING_TELEGRAM_PHONE"] = phone.strip()
    return secrets_path


def _read_secret_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _serialize_secret_values(values: dict[str, str]) -> str:
    rows = ["# 序影本地密钥。不要上传、截图或分享此文件。"]
    rows.extend(f"{key}={value}" for key, value in values.items())
    return "\n".join(rows) + "\n"


def save_immich_runtime_config(
    *,
    enabled: bool,
    base_url: str,
    api_key: str,
    library_id: str,
    external_library_path: str,
    auto_scan: bool,
    auto_album: bool,
    album_prefix: str,
    forwarded_library_id: str,
    forwarded_external_library_path: str,
    forwarded_auto_scan: bool,
    forwarded_auto_album: bool,
    forwarded_auto_archive: bool,
    forwarded_album_prefix: str,
    path: str | Path | None = None,
) -> Path:
    config_path = runtime_config_path(path)
    ensure_runtime_config(config_path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}
    raw["immich"] = {
        "enabled": enabled,
        "base_url": base_url.rstrip("/"),
        "library_id": library_id.strip(),
        "external_library_path": (
            external_library_path.rstrip("/") or "/external/xuying-main"
        ),
        "auto_scan": auto_scan,
        "auto_album": auto_album,
        "album_prefix": album_prefix.strip() or "序影",
        "forwarded_library_id": forwarded_library_id.strip(),
        "forwarded_external_library_path": (
            forwarded_external_library_path.rstrip("/")
            or "/external/xuying-forwarded"
        ),
        "forwarded_auto_scan": forwarded_auto_scan,
        "forwarded_auto_album": forwarded_auto_album,
        "forwarded_auto_archive": forwarded_auto_archive,
        "forwarded_album_prefix": (
            forwarded_album_prefix.strip() or "序影 · 机器人"
        ),
    }
    _atomic_write_yaml(config_path, raw)

    secrets_path = runtime_secrets_path(path)
    values = _read_secret_values(secrets_path)
    if api_key.strip():
        values["XUYING_IMMICH_API_KEY"] = api_key.strip()
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".secrets-", suffix=".tmp", dir=secrets_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_serialize_secret_values(values))
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, secrets_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    if api_key.strip():
        os.environ["XUYING_IMMICH_API_KEY"] = api_key.strip()
    return config_path


def _atomic_write_yaml(config_path: Path, raw: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".config-", suffix=".tmp", dir=config_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                raw,
                handle,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        os.replace(temporary_name, config_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def save_telegram_runtime_config(
    *,
    enabled: bool,
    proxy_url: str,
    channel: ChannelConfig | None = None,
    channels: list[ChannelConfig] | None = None,
    path: str | Path | None = None,
) -> Path:
    config_path = runtime_config_path(path)
    ensure_runtime_config(config_path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}
    telegram = raw.setdefault("telegram", {})
    telegram["enabled"] = enabled
    telegram["session_name"] = telegram.get("session_name") or "xuying"
    telegram["proxy_url"] = proxy_url.strip()
    resolved_channels = channels if channels is not None else ([channel] if channel else [])
    telegram["channels"] = [
        item.model_dump(mode="json") for item in resolved_channels
    ]

    _atomic_write_yaml(config_path, raw)
    return config_path


def save_telegram_bot_config(
    *,
    enabled: bool,
    token: str,
    allowed_user_ids: list[int],
    max_concurrent_downloads: int = 5,
    path: str | Path | None = None,
) -> Path:
    config_path = runtime_config_path(path)
    ensure_runtime_config(config_path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}
    telegram = raw.setdefault("telegram", {})
    telegram["bot_enabled"] = enabled
    telegram["bot_allowed_user_ids"] = sorted(set(allowed_user_ids))
    telegram["bot_max_concurrent_downloads"] = max(
        1, min(8, int(max_concurrent_downloads))
    )
    _atomic_write_yaml(config_path, raw)

    if token.strip():
        secrets_path = runtime_secrets_path(path)
        values = _read_secret_values(secrets_path)
        values["XUYING_TELEGRAM_BOT_TOKEN"] = token.strip()
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".secrets-", suffix=".tmp", dir=secrets_path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(_serialize_secret_values(values))
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, secrets_path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        os.environ["XUYING_TELEGRAM_BOT_TOKEN"] = token.strip()
    return config_path


def ensure_runtime_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        shutil.copyfile(_default_config_source(), path)
        os.chmod(path, 0o600)


def load_settings(path: str | Path | None = None) -> Settings:
    config_path = runtime_config_path(path)
    if not config_path.exists() and str(config_path).startswith("/config/"):
        ensure_runtime_config(config_path)
    if not config_path.exists():
        config_path = _default_config_source()
    raw: dict[str, Any]
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    raw.setdefault("telegram", {})
    storage = raw.setdefault("storage", {})
    environment_overrides = {
        "media_root": "XUYING_MEDIA_ROOT",
        "download_path": "XUYING_DOWNLOAD_PATH",
        "library_path": "XUYING_LIBRARY_PATH",
        "rebuild_path": "XUYING_REBUILD_PATH",
        "forwarded_path": "XUYING_FORWARDED_PATH",
        "forwarded_library_path": "XUYING_FORWARDED_LIBRARY_PATH",
    }
    for setting_name, environment_name in environment_overrides.items():
        value = os.getenv(environment_name, "").strip()
        if value:
            storage[setting_name] = value
    return Settings.model_validate(raw)
