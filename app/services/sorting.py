from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


MESSAGE_PREFIX_RE = re.compile(r"^\s*(?P<id>\d+)\s*[-_]\s*(?P<name>.+)$")
DATED_MEDIA_SEQUENCE_RE = re.compile(
    r"(?i)(?P<prefix>IMG|VID|PXL|MOV|DMG)[-_]"
    r"(?P<date>\d{8})[-_](?P<time>\d{6})"
    r"(?:[-_](?P<sequence>\d{1,6}))?"
)
CAMERA_SEQUENCE_RE = re.compile(
    r"(?i)(?P<prefix>IMG|DSC|DSCF|DJI|VID|PXL|DMG)[-_]?(?P<index>\d{3,8})"
)


class SortableMedia(Protocol):
    id: int
    original_filename: str
    original_order: int
    camera_prefix: str | None
    camera_index: int | None


@dataclass(frozen=True)
class FilenameMetadata:
    message_id: int | None
    original_name: str
    camera_prefix: str | None
    camera_index: int | None


def parse_filename(filename: str) -> FilenameMetadata:
    basename = Path(filename).name
    prefix_match = MESSAGE_PREFIX_RE.match(basename)
    message_id = int(prefix_match.group("id")) if prefix_match else None
    original_name = prefix_match.group("name") if prefix_match else basename
    stem = Path(original_name).stem
    dated_match = DATED_MEDIA_SEQUENCE_RE.search(stem)
    camera_match = CAMERA_SEQUENCE_RE.search(stem)
    if dated_match:
        # YYYYMMDDHHMMSS + a four-digit tie breaker remains below SQLite's
        # signed 64-bit integer limit and preserves chronological order.
        sequence = (dated_match.group("sequence") or "0").zfill(4)[-4:]
        camera_prefix = f"{dated_match.group('prefix').upper()}_DATETIME"
        camera_index = int(
            f"{dated_match.group('date')}{dated_match.group('time')}{sequence}"
        )
    else:
        camera_prefix = (
            camera_match.group("prefix").upper() if camera_match else None
        )
        camera_index = (
            int(camera_match.group("index")) if camera_match else None
        )
    return FilenameMetadata(
        message_id=message_id,
        original_name=original_name,
        camera_prefix=camera_prefix,
        camera_index=camera_index,
    )


def stable_camera_sort(items: list[SortableMedia]) -> list[SortableMedia]:
    """只重排有充分依据的相机序号位置；无法识别的文件保持原槽位。

    这避免乱码文件被全部推到组尾，也保留 Telegram 原始顺序作为回退。
    """
    ordered = sorted(items, key=lambda item: item.original_order)
    positions_by_prefix: dict[str, list[int]] = {}
    for position, item in enumerate(ordered):
        if item.camera_prefix and item.camera_index is not None:
            positions_by_prefix.setdefault(item.camera_prefix, []).append(position)

    result = list(ordered)
    for positions in positions_by_prefix.values():
        if len(positions) < 2:
            continue
        candidates = [ordered[position] for position in positions]
        # 相机序号重复在这个库里是常态：一个 Live Photo 会同时产生
        # IMG_1234.HEIC 与 IMG_1234.MOV，两者解析出同一个序号。早先遇到
        # 重复就整组放弃排序，导致成对文件较多的人物目录始终停留在
        # Telegram 原始消息顺序；改为用消息序号作为次级排序键。
        candidates.sort(key=lambda item: (item.camera_index, item.original_order))
        for position, item in zip(positions, candidates, strict=True):
            result[position] = item
    return result
