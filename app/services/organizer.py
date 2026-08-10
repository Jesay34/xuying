from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import MediaFile, MediaGroup
from app.services.content_rules import write_xmp_sidecar
from app.services.sorting import stable_camera_sort

logger = logging.getLogger(__name__)
SAFE_SEGMENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class OrganizerError(RuntimeError):
    pass


def safe_segment(value: str, fallback: str) -> str:
    value = SAFE_SEGMENT_RE.sub("_", value).strip(" .")
    return value[:120] or fallback


def assert_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    resolved_root = root.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise OrganizerError(f"路径超出允许目录：{resolved}")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    resolved_root = root.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents


def assert_media_source(path: Path, settings: Settings) -> Path:
    """Allow new Xuying downloads and legacy Hermes files, never generated output."""
    resolved = assert_within(path, settings.storage.media_root)
    blocked_roots = (
        settings.storage.library_path,
        settings.storage.rebuild_path,
    )
    if any(_is_within(resolved, blocked_root) for blocked_root in blocked_roots):
        raise OrganizerError("拒绝把序影整理输出或重构任务目录再次作为原始媒体处理")
    return resolved


def _target_directory(settings: Settings, group: MediaGroup) -> Path:
    channel = safe_segment(str(group.chat_id), "unknown-channel")
    # 目录身份保持稳定；同一人物后续新增消息时不需要改名或迁移目录。
    group_name = safe_segment(group.public_id, f"G{group.start_message_id}")
    if group.reason == "advertisement":
        return (
            settings.storage.library_path
            / channel
            / "_advertisements"
            / group_name
        )
    if group.reason == "channel_marker":
        return settings.storage.library_path / channel / "subjects" / group_name
    return settings.storage.library_path / channel / "groups" / group_name


def organize_group(db: Session, settings: Settings, group_id: int) -> MediaGroup:
    group = db.get(MediaGroup, group_id)
    if not group:
        raise OrganizerError("资源组不存在")

    files = list(
        db.scalars(
            select(MediaFile)
            .where(MediaFile.group_id == group_id)
            .order_by(MediaFile.original_order)
        )
    )
    sorted_files = (
        stable_camera_sort(files)
        if settings.organizer.camera_sequence_sort
        else sorted(files, key=lambda item: item.original_order)
    )
    target_dir = assert_within(
        _target_directory(settings, group), settings.storage.library_path
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    # A marker group is immutable after the next cover marker closes it.
    # Generate XMP in the same transaction as its hardlinks so "organized"
    # always means that Immich can consume a stable internal order.
    dated = [
        item.message.telegram_date
        for item in sorted_files
        if item.message is not None and item.message.telegram_date is not None
    ]
    anchor = max(dated) if dated else datetime.now(timezone.utc)
    generate_xmp = settings.organizer.generate_xmp or group.reason == "channel_marker"

    for display_order, media in enumerate(sorted_files, start=1):
        source = assert_media_source(Path(media.original_path), settings)
        if not source.is_file():
            media.state = "error"
            media.error = "原文件不存在"
            continue
        target_name = f"{display_order:04d}_{media.saved_filename}"
        target = assert_within(target_dir / target_name, settings.storage.library_path)
        if target.exists():
            if os.path.samefile(source, target):
                media.state = "organized"
                media.link_path = str(target)
                media.display_order = display_order
                if generate_xmp:
                    write_xmp_sidecar(
                        target, anchor - timedelta(seconds=display_order - 1)
                    )
                continue
            raise OrganizerError(f"目标文件已存在且不是同一硬链接：{target}")
        try:
            os.link(source, target)
        except OSError as exc:
            media.state = "error"
            media.error = f"硬链接失败（请确认两个目录位于同一文件系统）：{exc}"
            logger.exception("Failed to hardlink %s", source)
            continue
        media.display_order = display_order
        media.link_path = str(target)
        if generate_xmp:
            write_xmp_sidecar(
                target, anchor - timedelta(seconds=display_order - 1)
            )
        media.state = "organized"
        media.error = None

    if files and all(item.state == "organized" for item in files):
        group.status = "organized"
        group.output_path = str(target_dir)
    elif any(item.state == "error" for item in files):
        group.status = "error"
    db.commit()
    return group
