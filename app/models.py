from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_read_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    messages: Mapped[list["TelegramMessage"]] = relationship(back_populates="channel")


class TelegramMessage(Base):
    __tablename__ = "telegram_messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    media_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    caption: Mapped[str | None] = mapped_column(Text)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    channel: Mapped[Channel] = relationship(back_populates="messages")
    files: Mapped[list["MediaFile"]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )


class MediaGroup(Base):
    __tablename__ = "media_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    telegram_media_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    start_message_id: Mapped[int] = mapped_column(BigInteger)
    end_message_id: Mapped[int] = mapped_column(BigInteger)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    reason: Mapped[str] = mapped_column(String(255), default="telegram_media_group")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    output_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    files: Mapped[list["MediaFile"]] = relationship(back_populates="group")


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id_fk: Mapped[int] = mapped_column(ForeignKey("telegram_messages.id"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("media_groups.id"))
    original_path: Mapped[str] = mapped_column(Text, unique=True)
    original_filename: Mapped[str] = mapped_column(String(512))
    saved_filename: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    exif_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    camera_prefix: Mapped[str | None] = mapped_column(String(32))
    camera_index: Mapped[int | None] = mapped_column(Integer)
    original_order: Mapped[int] = mapped_column(Integer)
    display_order: Mapped[int] = mapped_column(Integer)
    link_path: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(32), default="downloaded", index=True)
    error: Mapped[str | None] = mapped_column(Text)

    message: Mapped[TelegramMessage] = relationship(back_populates="files")
    group: Mapped[MediaGroup | None] = relationship(back_populates="files")
    immich_sync: Mapped["ImmichSync | None"] = relationship(
        back_populates="file", uselist=False, cascade="all, delete-orphan"
    )


class ImmichSync(Base):
    __tablename__ = "immich_sync"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id"), unique=True)
    asset_id: Mapped[str | None] = mapped_column(String(128), index=True)
    album_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    file: Mapped[MediaFile] = relationship(back_populates="immich_sync")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LiveDownloadItem(Base):
    """Persistent queue item for media received by realtime channel listeners."""

    __tablename__ = "live_download_items"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    media_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    force_advertisement: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RebuildItem(Base):
    """Global history-download index used across overlapping rebuild tasks."""

    __tablename__ = "rebuild_items"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    media_group_id: Mapped[str | None] = mapped_column(String(64), index=True)
    telegram_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    caption: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(String(512))
    saved_filename: Mapped[str] = mapped_column(String(512))
    source_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
