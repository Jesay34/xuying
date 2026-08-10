from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthResponse(BaseModel):
    status: str
    version: str
    telegram_enabled: bool
    immich_enabled: bool


class GroupSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    public_id: str
    chat_id: int
    title: str | None
    start_message_id: int
    end_message_id: int
    confidence: float
    reason: str
    status: str
    created_at: datetime
    file_count: int = 0


class DashboardStats(BaseModel):
    channels: int
    messages: int
    media_files: int
    groups: int
    pending_groups: int
    organized_groups: int
    failed_files: int


class TelegramCodeRequest(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=20, max_length=128)
    phone: str = Field(min_length=6, max_length=32)
    proxy_url: str = Field(default="", max_length=512)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        normalized = value.replace(" ", "").replace("-", "")
        if not normalized.startswith("+") or not normalized[1:].isdigit():
            raise ValueError("手机号必须包含国家区号，例如 +8613800000000")
        return normalized

    @field_validator("api_hash")
    @classmethod
    def validate_api_hash(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) != 32 or any(
            character not in "0123456789abcdefABCDEF" for character in normalized
        ):
            raise ValueError("API Hash 应为 Telegram 提供的 32 位十六进制字符串")
        return normalized


class TelegramCodeConfirm(BaseModel):
    code: str = Field(min_length=3, max_length=16)

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = value.replace(" ", "").replace("-", "")
        if not normalized.isdigit():
            raise ValueError("验证码只能包含数字")
        return normalized


class TelegramPasswordConfirm(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class TelegramActivateRequest(BaseModel):
    channel_name: str = Field(default="我的频道", min_length=1, max_length=100)
    chat_id: int
    start_message_id: int = Field(default=0, ge=0)
    start_mode: str = Field(default="message_id")
    start_date: date | None = None
    proxy_url: str = Field(default="", max_length=512)
    grouping_mode: str = Field(default="telegram_album")
    marker_text: str = Field(default="1", max_length=32)
    advertisement_policy: str = Field(default="quarantine")
    advertisement_keywords: list[str] = Field(default_factory=list)
    display_spacing_hours: int = Field(default=24, ge=1, le=168)
    timeline_mode: str = Field(default="album")
    display_order: int = Field(default=0, ge=0, le=9999)
    max_concurrent_downloads: int = Field(default=5, ge=1, le=8)


class TelegramProxyUpdate(BaseModel):
    proxy_url: str = Field(default="", max_length=512)


class TelegramBotRequest(BaseModel):
    enabled: bool = True
    token: str = Field(default="", max_length=256)
    max_concurrent_downloads: int = Field(default=5, ge=1, le=8)


class TelegramChannelRequest(BaseModel):
    channel_name: str = Field(min_length=1, max_length=100)
    chat_id: int
    start_message_id: int = Field(default=0, ge=0)
    start_mode: str = Field(default="message_id")
    start_date: date | None = None
    enabled: bool = True
    grouping_mode: str = Field(default="telegram_album")
    marker_text: str = Field(default="1", max_length=32)
    advertisement_policy: str = Field(default="quarantine")
    advertisement_keywords: list[str] = Field(default_factory=list)
    display_spacing_hours: int = Field(default=24, ge=1, le=168)
    timeline_mode: str = Field(default="album")
    display_order: int = Field(default=0, ge=0, le=9999)
    max_concurrent_downloads: int = Field(default=5, ge=1, le=8)


class HistoryRebuildRequest(BaseModel):
    chat_id: int
    channel_name: str = Field(default="我的频道", min_length=1, max_length=100)
    start_date: date
    end_date: date
    grouping_mode: str = Field(default="telegram_album")
    marker_text: str = Field(default="1", max_length=32)
    advertisement_policy: str = Field(default="quarantine")
    display_spacing_hours: int = Field(default=24, ge=1, le=168)
    timeline_mode: str = Field(default="album")
    generate_xmp: bool = True
    max_concurrent_downloads: int = Field(default=5, ge=1, le=8)


class ImmichSettingsRequest(BaseModel):
    enabled: bool = True
    base_url: str = Field(min_length=4, max_length=512)
    api_key: str = Field(default="", max_length=1024)
    library_id: str = Field(default="", max_length=100)
    external_library_path: str = Field(
        default="/external/xuying-main", max_length=1024
    )
    auto_scan: bool = False
    auto_album: bool = True
    album_prefix: str = Field(default="序影", min_length=1, max_length=100)
    forwarded_library_id: str = Field(default="", max_length=100)
    forwarded_external_library_path: str = Field(
        default="/external/xuying-forwarded", max_length=1024
    )
    forwarded_auto_scan: bool = True
    forwarded_auto_album: bool = True
    forwarded_auto_archive: bool = True
    forwarded_album_prefix: str = Field(
        default="序影 · 机器人", min_length=1, max_length=100
    )
