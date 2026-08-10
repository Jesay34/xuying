from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


SPACE_RE = re.compile(r"\s+")
STRONG_AD_PATTERNS = (
    re.compile(r"https?://t\.me/", re.IGNORECASE),
    re.compile(r"(加入|进入|联系).{0,8}(群|频道|机器人)"),
    re.compile(r"(永久|终身|付费).{0,8}(会员|观看|资源)"),
    re.compile(r"(自助下单|售后|优惠价|恢复原价|名额|推广|广告)"),
)


@dataclass
class ContentBatch:
    kind: str
    records: list[dict]
    reason: str
    confidence: float
    marker_message_id: int | None = None


def normalize_caption(value: str | None) -> str:
    return SPACE_RE.sub("", value or "").strip()


def is_marker_caption(value: str | None, marker_text: str = "1") -> bool:
    return normalize_caption(value) == normalize_caption(marker_text)


def advertisement_score(
    value: str | None,
    keywords: Iterable[str],
) -> int:
    text = value or ""
    normalized = text.casefold()
    keyword_hits = sum(
        1 for keyword in keywords if keyword and keyword.casefold() in normalized
    )
    strong_hits = sum(1 for pattern in STRONG_AD_PATTERNS if pattern.search(text))
    return keyword_hits + strong_hits * 2


def is_advertisement_caption(
    value: str | None,
    keywords: Iterable[str],
) -> bool:
    # 需要多个信号才自动隔离，宁愿留下疑似广告，也不误删正常资源。
    return advertisement_score(value, keywords) >= 3


def _telegram_units(records: list[dict]) -> list[list[dict]]:
    units: dict[str, list[dict]] = {}
    for record in sorted(records, key=lambda item: int(item["message_id"])):
        key = (
            f"album:{record['media_group_id']}"
            if record.get("media_group_id")
            else f"message:{record['message_id']}"
        )
        units.setdefault(key, []).append(record)
    return list(units.values())


def segment_records(
    records: list[dict],
    *,
    grouping_mode: str,
    marker_text: str,
    advertisement_keywords: Iterable[str],
) -> list[ContentBatch]:
    units = _telegram_units(records)
    if grouping_mode != "marker":
        return [
            ContentBatch(
                kind="advertisement"
                if any(
                    is_advertisement_caption(item.get("caption"), advertisement_keywords)
                    for item in unit
                )
                else "telegram_group",
                records=unit,
                reason="advertisement_keywords"
                if any(
                    is_advertisement_caption(item.get("caption"), advertisement_keywords)
                    for item in unit
                )
                else "telegram_media_group",
                confidence=0.92
                if any(
                    is_advertisement_caption(item.get("caption"), advertisement_keywords)
                    for item in unit
                )
                else 1.0,
            )
            for unit in units
        ]

    batches: list[ContentBatch] = []
    current_subject: ContentBatch | None = None
    for unit in units:
        captions = [item.get("caption") for item in unit]
        marker = next(
            (
                item
                for item in unit
                if is_marker_caption(item.get("caption"), marker_text)
            ),
            None,
        )
        is_ad = any(
            is_advertisement_caption(caption, advertisement_keywords)
            for caption in captions
        )
        if marker:
            current_subject = ContentBatch(
                kind="subject",
                records=list(unit),
                reason="channel_marker",
                confidence=0.99,
                marker_message_id=int(marker["message_id"]),
            )
            batches.append(current_subject)
        elif is_ad:
            batches.append(
                ContentBatch(
                    kind="advertisement",
                    records=list(unit),
                    reason="advertisement_keywords",
                    confidence=0.92,
                )
            )
        elif current_subject:
            current_subject.records.extend(unit)
        else:
            batches.append(
                ContentBatch(
                    kind="unassigned",
                    records=list(unit),
                    reason="before_first_marker",
                    confidence=0.6,
                )
            )
    return batches


def display_datetimes(
    batches: list[ContentBatch],
    *,
    anchor: datetime,
    spacing_hours: int,
) -> dict[int, datetime]:
    visible = [
        batch for batch in batches if batch.kind not in {"advertisement"}
    ]
    result: dict[int, datetime] = {}
    for index, batch in enumerate(visible):
        # 最新批次获得最新时间；超过 30 组时自然跨月，不受月份天数限制。
        offset = len(visible) - index - 1
        result[id(batch)] = anchor - timedelta(hours=offset * spacing_hours)
    return result


def write_xmp_sidecar(media_path: Path, display_time: datetime) -> Path:
    value = display_time.isoformat(timespec="seconds")
    sidecar = media_path.with_name(media_path.name + ".xmp")
    sidecar.write_text(
        f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
      xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
      xmlns:exif="http://ns.adobe.com/exif/1.0/"
      xmp:CreateDate="{value}"
      xmp:ModifyDate="{value}"
      photoshop:DateCreated="{value}"
      exif:DateTimeOriginal="{value}" />
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
""",
        encoding="utf-8",
    )
    return sidecar
