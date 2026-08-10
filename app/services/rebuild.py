from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time as monotonic_time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from telethon.errors import (
    BadRequestError,
    FileReferenceExpiredError,
    FloodWaitError,
    RpcCallFailError,
    ServerError,
    TimedOutError,
)

from app.config import ChannelConfig, Settings
from app.models import MediaFile, MediaGroup, RebuildItem, Task, TelegramMessage
from app.services.organizer import (
    OrganizerError,
    assert_media_source,
    assert_within,
    safe_segment,
)
from app.services.content_rules import (
    display_datetimes,
    is_marker_caption,
    segment_records,
    write_xmp_sidecar,
)
from app.services.sorting import parse_filename, stable_camera_sort
from app.services.timezone_utils import configured_timezone, local_month
from app.services.telegram import TelegramService, TelegramSetupError

logger = logging.getLogger(__name__)


# A proxy or long-lived MTProto connection can close between two packets.  Python
# reports that case as IncompleteReadError/EOFError rather than OSError, so it must
# be treated like every other recoverable Telegram transport failure.
TRANSIENT_TELEGRAM_ERRORS = (
    asyncio.IncompleteReadError,
    EOFError,
    ConnectionError,
    OSError,
    TimedOutError,
    RpcCallFailError,
    ServerError,
)


@dataclass
class RebuildMedia:
    id: int
    original_filename: str
    saved_filename: str
    source_path: str
    original_order: int
    camera_prefix: str | None
    camera_index: int | None


class HistoryRebuildService:
    def __init__(
        self,
        settings: Settings,
        session_factory: sessionmaker[Session],
        telegram: TelegramService,
        immich=None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.telegram = telegram
        self.immich = immich
        self._running: dict[int, asyncio.Task] = {}
        # Historical jobs are intentionally serialized. This prevents two overlapping
        # date ranges from downloading the same Telegram message at the same time.
        self._execution_lock = asyncio.Lock()
        # File workers share one Telegram cooldown. If any worker receives FloodWait,
        # all workers stop issuing new Telegram requests until the limit expires.
        self._telegram_cooldown_until = 0.0
        self._fallback_download_locks: dict[
            tuple[int, int], asyncio.Lock
        ] = {}
        self._reconcile_tasks: dict[int, asyncio.Task] = {}
        self._reconcile_refresh: dict[int, bool] = {}
        self._channel_reconcile_locks: dict[int, asyncio.Lock] = {}
        self._connection_recovery_lock = asyncio.Lock()
        self._order_migration_task: asyncio.Task | None = None
        self._album_membership_task: asyncio.Task | None = None

    def _message_download_lock(
        self, chat_id: int, message_id: int
    ) -> asyncio.Lock:
        shared_lock = getattr(self.telegram, "media_download_lock", None)
        if shared_lock:
            return shared_lock(chat_id, message_id)
        key = (int(chat_id), int(message_id))
        return self._fallback_download_locks.setdefault(key, asyncio.Lock())

    async def start(self) -> None:
        self._backfill_global_index()
        self._repair_completed_task_counts()
        self._order_migration_task = asyncio.create_task(
            self._ensure_content_order_migration()
        )
        self._album_membership_task = asyncio.create_task(
            self._ensure_album_membership_correction()
        )
        if self.telegram.status != "running":
            return
        with self.session_factory() as db:
            task_ids = list(
                db.scalars(
                    select(Task.id).where(
                        Task.kind == "history_rebuild",
                        Task.status.in_(["queued", "running"]),
                    )
                )
            )
        for task_id in task_ids:
            self._schedule(task_id)

    def migrate_raw_channel_aliases(self) -> dict[str, int]:
        """Merge display-name aliases into the configured Chat-ID directory.

        Older rebuild forms allowed an arbitrary label such as ``主监听频道``
        to become a physical raw folder.  The database still knows the real
        Chat ID, so migration is deterministic and never needs image matching.
        """
        moved = 0
        reused = 0
        conflicts = 0
        path_updates: dict[str, str] = {}
        alias_directories: set[Path] = set()
        legacy_aliases: dict[int, set[str]] = {}

        def safe_label(value: str, fallback: str) -> str:
            return "".join(
                character if character.isalnum() or character in "-_" else "_"
                for character in value
            ).strip("_") or fallback

        def same_content(left: Path, right: Path) -> bool:
            if left.stat().st_size != right.stat().st_size:
                return False
            with left.open("rb") as first, right.open("rb") as second:
                while True:
                    first_chunk = first.read(1024 * 1024)
                    second_chunk = second.read(1024 * 1024)
                    if first_chunk != second_chunk:
                        return False
                    if not first_chunk:
                        return True

        channel_by_id = {
            int(channel.chat_id): channel
            for channel in self.settings.telegram.channels
        }
        with self.session_factory() as db:
            # Old task cards also carried the free-form label. Normalize those
            # labels so the UI, resume logic and future maintenance all show the
            # configured channel identity.
            task_rows = list(
                db.scalars(select(Task).where(Task.kind == "history_rebuild"))
            )
            for task_row in task_rows:
                payload = json.loads(task_row.payload_json or "{}")
                try:
                    channel = channel_by_id.get(int(payload.get("chat_id")))
                except (TypeError, ValueError):
                    channel = None
                if channel and payload.get("channel_name") != channel.name:
                    old_label = str(payload.get("channel_name") or "").strip()
                    if old_label:
                        legacy_aliases.setdefault(int(channel.chat_id), set()).add(
                            safe_label(old_label, str(channel.chat_id))
                        )
                    payload["channel_name"] = channel.name
                    payload["canonical_channel_name"] = channel.name
                    task_row.payload_json = json.dumps(payload, ensure_ascii=False)

            indexed_rows = list(db.scalars(select(RebuildItem)))
            live_rows = list(
                db.execute(
                    select(TelegramMessage, MediaFile).join(
                        MediaFile,
                        MediaFile.message_id_fk == TelegramMessage.id,
                    )
                )
            )
            records: list[tuple[int, object, str, datetime | None]] = [
                (
                    int(row.chat_id),
                    row,
                    "source_path",
                    row.telegram_date,
                )
                for row in indexed_rows
            ]
            records.extend(
                (
                    int(message.chat_id),
                    media,
                    "original_path",
                    message.telegram_date,
                )
                for message, media in live_rows
            )

            for chat_id, row, path_attribute, telegram_date in records:
                channel = channel_by_id.get(chat_id)
                if channel is None:
                    continue
                source_text = str(getattr(row, path_attribute) or "")
                source = Path(source_text)
                try:
                    relative = source.relative_to(
                        self.settings.storage.download_path
                    )
                except ValueError:
                    continue
                if len(relative.parts) < 2:
                    continue
                canonical_name = self.telegram._safe_channel_name(channel)
                current_month = (
                    relative.parts[1]
                    if len(relative.parts) >= 3
                    and re.fullmatch(r"\d{4}_\d{2}", relative.parts[1])
                    else None
                )
                expected_month = (
                    local_month(telegram_date, self.settings.app.timezone)
                    if telegram_date
                    else current_month or "unknown_month"
                )

                # The common case must not touch the filesystem.  This keeps
                # startup fast even with hundreds of thousands of indexed files.
                if (
                    relative.parts[0] == canonical_name
                    and current_month == expected_month
                ):
                    continue
                if not source.is_file():
                    if source_text in path_updates:
                        setattr(row, path_attribute, path_updates[source_text])
                    continue

                target_dir = assert_within(
                    self.settings.storage.download_path
                    / canonical_name
                    / expected_month,
                    self.settings.storage.download_path,
                )
                target_dir.mkdir(parents=True, exist_ok=True)
                target = assert_within(target_dir / source.name, target_dir)
                try:
                    alias_directories.add(source.parent)
                    alias_directories.add(
                        self.settings.storage.download_path / relative.parts[0]
                    )
                    if target.exists():
                        if os.path.samefile(source, target) or same_content(source, target):
                            source.unlink()
                            reused += 1
                        else:
                            conflicts += 1
                            logger.warning(
                                "Raw path migration kept conflicting file: %s",
                                source,
                            )
                            continue
                    else:
                        shutil.move(str(source), str(target))
                        moved += 1
                    sidecar = source.with_name(source.name + ".xmp")
                    target_sidecar = target.with_name(target.name + ".xmp")
                    if sidecar.is_file():
                        if target_sidecar.exists():
                            sidecar.unlink()
                        else:
                            shutil.move(str(sidecar), str(target_sidecar))
                    path_updates[source_text] = str(target)
                    setattr(row, path_attribute, str(target))
                except OSError:
                    conflicts += 1
                    logger.exception("Raw path migration failed for %s", source)
            db.commit()

        # Also merge any leftover files that predate the global index.  The
        # alias-to-Chat-ID mapping comes only from existing task records, so an
        # unrelated folder can never be guessed or moved.
        for chat_id, aliases in legacy_aliases.items():
            channel = channel_by_id[chat_id]
            canonical_name = self.telegram._safe_channel_name(channel)
            for alias in aliases:
                if alias == canonical_name:
                    continue
                alias_root = assert_within(
                    self.settings.storage.download_path / alias,
                    self.settings.storage.download_path,
                )
                if not alias_root.is_dir():
                    continue
                for source in sorted(alias_root.rglob("*")):
                    if not source.is_file():
                        continue
                    relative = source.relative_to(alias_root)
                    target = assert_within(
                        self.settings.storage.download_path
                        / canonical_name
                        / relative,
                        self.settings.storage.download_path,
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    alias_directories.add(source.parent)
                    alias_directories.add(alias_root)
                    try:
                        if target.exists():
                            if os.path.samefile(source, target) or same_content(
                                source, target
                            ):
                                source.unlink()
                                reused += 1
                            else:
                                conflicts += 1
                                logger.warning(
                                    "Raw alias migration kept conflicting file: %s",
                                    source,
                                )
                                continue
                        else:
                            shutil.move(str(source), str(target))
                            moved += 1
                        path_updates[str(source)] = str(target)
                    except OSError:
                        conflicts += 1
                        logger.exception(
                            "Raw alias fallback migration failed for %s", source
                        )

        # Paused task manifests also contain source paths.  Update them so a
        # resumed task can continue without redownloading moved files.
        if path_updates:
            for manifest in self.settings.storage.rebuild_path.rglob("manifest.jsonl"):
                try:
                    records = self._load_manifest(manifest)
                    changed = False
                    for record in records:
                        old_path = str(record.get("source_path") or "")
                        if old_path in path_updates:
                            record["source_path"] = path_updates[old_path]
                            changed = True
                    if changed:
                        temporary = manifest.with_suffix(".jsonl.tmp")
                        temporary.write_text(
                            "".join(
                                json.dumps(record, ensure_ascii=False) + "\n"
                                for record in records
                            ),
                            encoding="utf-8",
                        )
                        os.replace(temporary, manifest)
                except Exception:
                    logger.exception("Unable to update migrated manifest %s", manifest)

        for directory in sorted(
            alias_directories, key=lambda item: len(item.parts), reverse=True
        ):
            try:
                assert_within(directory, self.settings.storage.download_path).rmdir()
            except OSError:
                # A directory containing an unindexed/conflicting file is kept.
                pass
        return {"moved": moved, "reused": reused, "conflicts": conflicts}

    def _repair_completed_task_counts(self) -> None:
        """Correct alpha.22 task cards that stored channel-wide group totals."""
        with self.session_factory() as db:
            rows = list(
                db.scalars(
                    select(Task).where(
                        Task.kind == "history_rebuild",
                        Task.status == "completed",
                    )
                )
            )
            changed = False
            for row in rows:
                payload = json.loads(row.payload_json or "{}")
                task_root = payload.get("task_root")
                if not task_root:
                    continue
                manifest = Path(task_root) / "manifest.jsonl"
                records = self._load_manifest(manifest)
                if not records:
                    continue
                counts = self._count_batches(
                    records,
                    grouping_mode=payload.get(
                        "grouping_mode", "telegram_album"
                    ),
                    marker_text=payload.get("marker_text", "1"),
                    advertisement_policy=payload.get(
                        "advertisement_policy", "quarantine"
                    ),
                )
                if any(payload.get(key) != value for key, value in counts.items()):
                    payload.update(counts)
                    row.payload_json = json.dumps(payload, ensure_ascii=False)
                    changed = True
            if changed:
                db.commit()

    async def stop(self) -> None:
        if self._order_migration_task and not self._order_migration_task.done():
            self._order_migration_task.cancel()
        if self._album_membership_task and not self._album_membership_task.done():
            self._album_membership_task.cancel()
        for task in self._running.values():
            task.cancel()
        for task in self._reconcile_tasks.values():
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        if self._reconcile_tasks:
            await asyncio.gather(
                *self._reconcile_tasks.values(), return_exceptions=True
            )
        if self._order_migration_task:
            await asyncio.gather(
                self._order_migration_task, return_exceptions=True
            )
        if self._album_membership_task:
            await asyncio.gather(
                self._album_membership_task, return_exceptions=True
            )
        self._running.clear()
        self._reconcile_tasks.clear()
        self._reconcile_refresh.clear()

    async def _ensure_content_order_migration(self) -> None:
        """Repair existing albums once after installing this release."""
        marker = self.settings.storage.data_path / ".alpha48-immich-v3-album-membership"
        if marker.exists():
            return
        try:
            await asyncio.sleep(8)
            for channel in self.settings.telegram.channels:
                if not channel.enabled:
                    continue
                while self._active_history_conflicts_with_live(channel.chat_id):
                    await asyncio.sleep(3)
                await self.reconcile_channel(
                    channel.chat_id,
                    refresh_xmp=True,
                    force_generate_xmp=True,
                    exclude_message_ids=self._active_history_partial_ids(
                        channel.chat_id
                    ),
                )
            marker.write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Leave the marker absent so the next restart retries safely.
            logger.exception("一次性人物相册顺序迁移失败，将在下次启动重试")

    async def _ensure_album_membership_correction(self) -> None:
        """Re-verify album membership once after a membership-logic fix.

        Hardlinks, ordering and XMP are untouched by those fixes, so a full
        channel reconcile is unnecessary; only Immich membership is stale.
        """
        marker = (
            self.settings.storage.data_path
            / ".alpha51-immich-live-photo-motion-halves"
        )
        if marker.exists():
            return
        try:
            await asyncio.sleep(12)
            if self.immich is not None:
                self.immich.request("main", force=True)
            marker.write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("一次性人物相册成员校正未能启动，将在下次启动重试")

    async def create(
        self,
        *,
        chat_id: int,
        channel_name: str,
        start_date: date,
        end_date: date,
        grouping_mode: str = "telegram_album",
        marker_text: str = "1",
        advertisement_policy: str = "quarantine",
        display_spacing_hours: int = 24,
        timeline_mode: str = "album",
        generate_xmp: bool = True,
        max_concurrent_downloads: int = 5,
    ) -> Task:
        if end_date < start_date:
            raise TelegramSetupError("结束日期不能早于开始日期")
        if (end_date - start_date).days > 370:
            raise TelegramSetupError("单个重构任务最长支持 371 天，请按年份拆分")
        client = await self.telegram._ensure_client()
        if not await client.is_user_authorized():
            raise TelegramSetupError("Telegram 尚未登录")
        try:
            await client.get_entity(chat_id)
        except Exception as exc:
            raise TelegramSetupError(
                "无法访问这个频道，请检查 Chat ID 和账号权限"
            ) from exc

        monitored_channel = next(
            (
                channel
                for channel in self.settings.telegram.channels
                if int(channel.chat_id) == int(chat_id)
            ),
            None,
        )
        if monitored_channel is None:
            raise TelegramSetupError(
                "历史补全只能选择已配置频道，请先到“频道”页面添加该频道"
            )
        coordinated_with_live = monitored_channel is not None
        # Chat ID is the permanent identity. History and live downloads must
        # always share the configured name and grouping policy.
        channel_name = monitored_channel.name
        grouping_mode = monitored_channel.grouping_mode
        marker_text = monitored_channel.marker_text
        advertisement_policy = monitored_channel.advertisement_policy
        display_spacing_hours = monitored_channel.display_spacing_hours
        timeline_mode = monitored_channel.timeline_mode
        payload = {
            "chat_id": chat_id,
            "channel_name": channel_name.strip() or str(chat_id),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "downloaded": 0,
            "groups": 0,
            "subjects": 0,
            "advertisements": 0,
            "grouping_mode": grouping_mode,
            "marker_text": marker_text,
            "advertisement_policy": advertisement_policy,
            "display_spacing_hours": display_spacing_hours,
            "timeline_mode": timeline_mode,
            "generate_xmp": generate_xmp,
            "max_concurrent_downloads": max(
                1, min(8, int(max_concurrent_downloads))
            ),
            "coordinated_with_live": coordinated_with_live,
            "coordination_note": (
                "该频道正在实时监听：系统会按消息 ID 去重，"
                "并在下载后从频道总索引统一重算人物批次。"
                if coordinated_with_live
                else ""
            ),
        }
        if coordinated_with_live:
            payload["coordination_note"] = (
                "该频道正在实时监听：系统按频道与消息 ID 去重，并按人物起始标记、"
                "Telegram 媒体组和消息范围判断是否冲突。较早且不相交的补全不会阻塞"
                "当前人物整理；只有内容边界重叠时才在完成后统一重算受影响相册。"
            )
        with self.session_factory() as db:
            task_row = Task(
                kind="history_rebuild",
                status="queued",
                progress=0,
                payload_json=json.dumps(payload, ensure_ascii=False),
            )
            db.add(task_row)
            db.commit()
            db.refresh(task_row)
            task_id = task_row.id
        self._schedule(task_id)
        with self.session_factory() as db:
            return db.get(Task, task_id)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        with self.session_factory() as db:
            history_ids = list(
                db.scalars(
                    select(Task.id)
                    .where(Task.kind == "history_rebuild")
                    .order_by(Task.created_at.asc(), Task.id.asc())
                )
            )
            display_ids = {
                task_id: position
                for position, task_id in enumerate(history_ids, start=1)
            }
            rows = list(
                db.scalars(
                    select(Task)
                    .where(Task.kind == "history_rebuild")
                    .order_by(Task.created_at.desc())
                    .limit(limit)
                )
            )
            result = []
            for row in rows:
                item = self._serialize(row)
                item["display_id"] = display_ids.get(row.id, row.id)
                result.append(item)
            return result

    async def pause(self, task_id: int) -> dict:
        with self.session_factory() as db:
            row = db.get(Task, task_id)
            if not row or row.kind != "history_rebuild":
                raise TelegramSetupError("没有找到这个历史补全任务")
            if row.status not in {"queued", "scanning", "running", "organizing"}:
                raise TelegramSetupError("当前任务状态不能暂停")
            row.status = "paused"
            db.commit()
        running = self._running.get(task_id)
        if running and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        return {"id": task_id, "status": "paused", "message": "任务已暂停，可随时继续"}

    async def resume(self, task_id: int) -> dict:
        with self.session_factory() as db:
            row = db.get(Task, task_id)
            if not row or row.kind != "history_rebuild":
                raise TelegramSetupError("没有找到这个历史补全任务")
            if row.status not in {"paused", "failed", "cancelled"}:
                raise TelegramSetupError("只有暂停、失败或取消的任务可以继续")
            row.status = "queued"
            row.error = None
            row.finished_at = None
            db.commit()
        self._schedule(task_id)
        return {"id": task_id, "status": "queued", "message": "任务已进入继续队列"}

    async def cancel(self, task_id: int) -> dict:
        with self.session_factory() as db:
            row = db.get(Task, task_id)
            if not row or row.kind != "history_rebuild":
                raise TelegramSetupError("没有找到这个历史补全任务")
            if row.status == "completed":
                raise TelegramSetupError("已完成任务无需取消，可直接清除任务记录")
            row.status = "cancelled"
            row.finished_at = datetime.now(timezone.utc)
            db.commit()
        running = self._running.get(task_id)
        if running and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        return {
            "id": task_id,
            "status": "cancelled",
            "message": "任务已取消，已下载媒体仍安全保留",
        }

    async def delete(self, task_id: int) -> dict:
        running = self._running.get(task_id)
        if running and not running.done():
            raise TelegramSetupError("请先暂停或取消正在运行的任务")
        with self.session_factory() as db:
            row = db.get(Task, task_id)
            if not row or row.kind != "history_rebuild":
                raise TelegramSetupError("没有找到这个历史补全任务")
            if row.status in {"queued", "scanning", "running", "organizing"}:
                raise TelegramSetupError("请先暂停或取消正在运行的任务")
            db.delete(row)
            db.commit()
        return {
            "id": task_id,
            "message": "任务提示已清除；原始媒体和整理库没有删除",
        }

    async def repair_order(self, task_id: int) -> dict:
        """Recalculate a completed channel without moving existing Immich paths."""
        async with self._execution_lock:
            with self.session_factory() as db:
                row = db.get(Task, task_id)
                if not row or row.kind != "history_rebuild":
                    raise TelegramSetupError("没有找到这个历史补全任务")
                if row.status != "completed":
                    raise TelegramSetupError("只有已经完成的任务可以安全校正排序")
                payload = json.loads(row.payload_json or "{}")

            chat_id = int(payload["chat_id"])
            records = self._all_channel_records(chat_id)
            if not records:
                raise TelegramSetupError("没有找到可重新排序的原始媒体")
            library_dir = assert_within(
                self.settings.storage.library_path
                / safe_segment(str(chat_id), "unknown-channel"),
                self.settings.storage.library_path,
            )
            library_dir.mkdir(parents=True, exist_ok=True)
            result = self._build_library(
                records,
                library_dir,
                grouping_mode=payload.get("grouping_mode", "telegram_album"),
                marker_text=payload.get("marker_text", "1"),
                advertisement_policy=payload.get(
                    "advertisement_policy", "quarantine"
                ),
                display_spacing_hours=int(
                    payload.get("display_spacing_hours", 24)
                ),
                timeline_mode=payload.get("timeline_mode", "album"),
                generate_xmp=bool(payload.get("generate_xmp", True)),
                refresh_xmp=True,
                force_normalize=True,
            )
            payload["order_repaired_at"] = datetime.now(timezone.utc).isoformat()
            payload["order_repair_count"] = int(
                payload.get("order_repair_count", 0)
            ) + 1
            payload["library_groups_total"] = result["groups"]
            payload["library_subjects_total"] = result["subjects"]
            payload["library_advertisements_total"] = result["advertisements"]
            with self.session_factory() as db:
                row = db.get(Task, task_id)
                if row:
                    row.payload_json = json.dumps(payload, ensure_ascii=False)
                    row.error = None
                    db.commit()
            if self.immich is not None:
                self.immich.schedule_refresh(refresh_metadata=True)
            return {
                "id": task_id,
                "status": "completed",
                "message": (
                    "排序已安全重新计算；没有重新下载或移动已入库媒体。"
                    "已更新 XMP 和相册同步依据。"
                ),
                **result,
            }

    def _schedule(self, task_id: int) -> None:
        existing = self._running.get(task_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._run(task_id))
        self._running[task_id] = task
        task.add_done_callback(
            lambda completed, current_id=task_id: self._forget(current_id, completed)
        )

    def _forget(self, task_id: int, completed: asyncio.Task) -> None:
        if self._running.get(task_id) is completed:
            self._running.pop(task_id, None)

    async def _run(self, task_id: int) -> None:
        async with self._execution_lock:
            await self._run_locked(task_id)

    async def _run_locked(self, task_id: int) -> None:
        try:
            with self.session_factory() as db:
                task_row = db.get(Task, task_id)
                if not task_row:
                    return
                if task_row.status in {"paused", "cancelled", "completed"}:
                    return
                payload = json.loads(task_row.payload_json or "{}")
                task_row.status = "scanning"
                task_row.started_at = task_row.started_at or datetime.now(timezone.utc)
                task_row.error = None
                db.commit()

            chat_id = int(payload["chat_id"])
            start_date = date.fromisoformat(payload["start_date"])
            end_date = date.fromisoformat(payload["end_date"])
            local_zone = configured_timezone(self.settings.app.timezone)
            start_utc = datetime.combine(
                start_date, time.min, tzinfo=local_zone
            ).astimezone(timezone.utc)
            end_exclusive_utc = datetime.combine(
                end_date + timedelta(days=1), time.min, tzinfo=local_zone
            ).astimezone(timezone.utc)

            saved_task_root = payload.get("task_root")
            if saved_task_root:
                # alpha.5 中断的任务仍可从旧目录继续，避免重复下载。
                saved_path = Path(saved_task_root)
                try:
                    task_root = assert_within(
                        saved_path, self.settings.storage.rebuild_path
                    )
                except OrganizerError:
                    task_root = assert_within(
                        saved_path,
                        self.settings.storage.media_root / "xuying-rebuild",
                    )
            else:
                range_name = (
                    f"{start_date.isoformat()}_{end_date.isoformat()}_T{task_id:06d}"
                )
                task_root = assert_within(
                    self.settings.storage.rebuild_path
                    / safe_segment(str(chat_id), "unknown-channel")
                    / range_name,
                    self.settings.storage.rebuild_path,
                )
            # rebuild 目录只保存断点清单，但清单的父目录仍必须先创建。
            task_root.mkdir(parents=True, exist_ok=True)
            # A channel's Chat ID is its identity.  The rebuild form's display
            # label must never create a second raw folder for the same channel.
            # Prefer the persisted listening-channel name and only fall back to
            # the task label when this Chat ID has no configured channel.
            configured_channel = next(
                (
                    item
                    for item in self.settings.telegram.channels
                    if int(item.chat_id) == chat_id
                ),
                None,
            )
            raw_channel_segment = self.telegram._safe_channel_name(
                configured_channel
                or ChannelConfig(
                    name=str(payload.get("channel_name", chat_id)),
                    chat_id=chat_id,
                )
            )
            library_channel_segment = safe_segment(str(chat_id), "unknown-channel")
            library_dir = assert_within(
                self.settings.storage.library_path / library_channel_segment,
                self.settings.storage.library_path,
            )
            library_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = task_root / "manifest.jsonl"
            records = self._load_manifest(manifest_path)
            for record in records:
                if "owner" not in record:
                    indexed = self._get_index_record(
                        chat_id, int(record["message_id"])
                    )
                    record["owner"] = (
                        indexed.get("owner", "rebuild")
                        if indexed
                        else "rebuild"
                    )
            completed_message_ids = {int(item["message_id"]) for item in records}

            client = await self._ensure_client_with_retry(task_id, payload)
            entity = await self._get_entity_with_retry(
                client, chat_id, task_id, payload
            )
            # Only retain message IDs during the scan. Telegram media file references
            # are temporary; keeping hundreds of Message objects for a long-running
            # job makes the later references expire before their turn is downloaded.
            message_ids = await self._scan_message_ids_with_retry(
                client=client,
                entity=entity,
                start_utc=start_utc,
                end_exclusive_utc=end_exclusive_utc,
                task_id=task_id,
                payload=payload,
            )

            payload["total"] = len(message_ids)
            payload["message_id_min"] = min(message_ids) if message_ids else None
            payload["message_id_max"] = max(message_ids) if message_ids else None
            payload["pending"] = max(
                0, len(message_ids) - len(completed_message_ids)
            )
            payload["phase"] = "downloading"
            payload["task_root"] = str(task_root)
            with self.session_factory() as db:
                row = db.get(Task, task_id)
                if not row or row.status == "paused":
                    return
                row.status = "running"
                row.payload_json = json.dumps(payload, ensure_ascii=False)
                db.commit()

            active_downloads: dict[int, dict] = {}
            concurrency = max(
                1, min(8, int(payload.get("max_concurrent_downloads", 5)))
            )
            payload["max_concurrent_downloads"] = concurrency
            pending_ids = [
                message_id
                for message_id in message_ids
                if message_id not in completed_message_ids
            ]

            async def process_message_owned(
                message_id: int,
            ) -> tuple[dict, bool, int]:
                indexed = self._get_index_record(chat_id, message_id)
                if indexed and Path(indexed["source_path"]).is_file():
                    return indexed, True, 0

                # Refresh each message only when a worker is ready to download it.
                message = await self._get_fresh_message(
                    client, entity, message_id, task_id, payload
                )
                original_name = self.telegram._original_filename(message)
                raw_name_builder = getattr(
                    self.telegram, "_raw_saved_filename", None
                )
                logical_name_builder = getattr(
                    self.telegram, "_logical_saved_filename", None
                )
                if raw_name_builder is None:
                    saved_name = (
                        f"{message.id} - "
                        f"{self.telegram._safe_filename(original_name)}"
                    )
                else:
                    saved_name = raw_name_builder(
                        message,
                        original_name,
                        payload.get("marker_text", "1"),
                    )
                logical_saved_name = (
                    logical_name_builder(
                        message,
                        original_name,
                        payload.get("marker_text", "1"),
                    )
                    if logical_name_builder is not None
                    else f"{message.id} - {self.telegram._safe_filename(original_name)}"
                )
                local_message_date = message.date.astimezone(local_zone)
                raw_dir = assert_within(
                    self.settings.storage.download_path
                    / raw_channel_segment
                    / local_message_date.strftime("%Y_%m"),
                    self.settings.storage.download_path,
                )
                raw_dir.mkdir(parents=True, exist_ok=True)
                destination = assert_within(raw_dir / saved_name, raw_dir)
                existed_before = destination.is_file()
                started = monotonic_time.monotonic()
                if not existed_before:
                    partial = destination.with_name(
                        f".{destination.name}.T{task_id:06d}.part"
                    )
                    file_started = monotonic_time.monotonic()
                    last_report = [0.0]
                    last_received = [0]
                    last_speed_at = [file_started]
                    smoothed_speed = [0.0]

                    def report_download(received: int, total_bytes: int) -> None:
                        now = monotonic_time.monotonic()
                        if (
                            now - last_report[0] < 1.0
                            and received < total_bytes
                        ):
                            return
                        last_report[0] = now
                        sample_elapsed = max(now - last_speed_at[0], 0.001)
                        sample_bytes = max(0, int(received) - last_received[0])
                        sample_speed = sample_bytes / sample_elapsed
                        smoothed_speed[0] = (
                            sample_speed
                            if smoothed_speed[0] <= 0
                            else smoothed_speed[0] * 0.55 + sample_speed * 0.45
                        )
                        last_received[0] = int(received)
                        last_speed_at[0] = now
                        active_downloads[message_id] = {
                            "message_id": message_id,
                            "filename": saved_name,
                            "received": int(received),
                            "total": int(total_bytes or 0),
                            "speed_bps": int(smoothed_speed[0]),
                        }
                        payload["active_downloads"] = list(
                            active_downloads.values()
                        )
                        payload["active_download_count"] = len(active_downloads)
                        payload["speed_bps"] = sum(
                            int(item["speed_bps"])
                            for item in active_downloads.values()
                        )
                        self._update_progress(task_id, payload, len(records))

                    try:
                        if partial.exists():
                            partial.unlink()
                        message = await self._download_with_retry(
                            client=client,
                            entity=entity,
                            message=message,
                            partial=partial,
                            progress_callback=report_download,
                            task_id=task_id,
                            payload=payload,
                        )
                        if not partial.is_file():
                            raise RuntimeError(
                                f"消息 {message.id} 下载后未找到临时文件"
                            )
                        os.replace(partial, destination)
                    finally:
                        active_downloads.pop(message_id, None)
                        payload["active_downloads"] = list(
                            active_downloads.values()
                        )
                        payload["active_download_count"] = len(active_downloads)
                        if partial.exists():
                            partial.unlink()

                if not destination.is_file():
                    raise RuntimeError(f"消息 {message.id} 下载后未找到文件")
                size_bytes = destination.stat().st_size
                elapsed = max(monotonic_time.monotonic() - started, 0.001)
                reused = existed_before
                record = {
                    "chat_id": chat_id,
                    "message_id": int(message.id),
                    "media_group_id": (
                        str(message.grouped_id)
                        if getattr(message, "grouped_id", None)
                        else None
                    ),
                    "telegram_date": message.date.isoformat(),
                    "caption": getattr(message, "message", None),
                    "original_filename": original_name,
                    "saved_filename": logical_saved_name,
                    "source_path": str(destination),
                    "size_bytes": size_bytes,
                    "owner": "rebuild",
                }
                self._store_index(record)
                payload["current_file_speed_bps"] = (
                    0 if reused else size_bytes / elapsed
                )
                return record, reused, 0 if reused else size_bytes

            async def process_message(
                message_id: int,
            ) -> tuple[dict, bool, int]:
                async with self._message_download_lock(chat_id, message_id):
                    return await process_message_owned(message_id)

            # Maintain a sliding window: as soon as one file finishes, the next
            # begins, while the semaphore keeps the selected concurrency limit.
            semaphore = asyncio.Semaphore(concurrency)

            async def guarded_process(message_id: int):
                async with semaphore:
                    result = await process_message(message_id)
                    return message_id, result

            download_tasks = [
                asyncio.create_task(guarded_process(message_id))
                for message_id in pending_ids
            ]
            try:
                for completed in asyncio.as_completed(download_tasks):
                    message_id, (record, reused, new_bytes) = await completed
                    # The task manifest is a per-task checkpoint; the global index
                    # guarantees overlapping ranges reuse the same media.
                    self._append_manifest(manifest_path, record)
                    records.append(record)
                    completed_message_ids.add(message_id)
                    payload["downloaded"] = len(records)
                    payload["processed"] = len(records)
                    payload["pending"] = max(
                        0, len(message_ids) - len(records)
                    )
                    if reused:
                        payload["reused"] = int(payload.get("reused", 0)) + 1
                    else:
                        payload["new_downloads"] = int(
                            payload.get("new_downloads", 0)
                        ) + 1
                        partial_ids = payload.setdefault(
                            "partial_new_message_ids", []
                        )
                        if message_id not in partial_ids:
                            partial_ids.append(message_id)
                        payload["downloaded_bytes"] = int(
                            payload.get("downloaded_bytes", 0)
                        ) + int(new_bytes)
                    payload["speed_bps"] = sum(
                        int(item.get("speed_bps", 0))
                        for item in active_downloads.values()
                    )
                    average_size = (
                        int(payload.get("downloaded_bytes", 0))
                        / max(1, int(payload.get("new_downloads", 0)))
                    )
                    payload["eta_seconds"] = (
                        payload["pending"] * average_size / payload["speed_bps"]
                        if payload["speed_bps"] > 0 and average_size > 0
                        else None
                    )
                    self._update_progress(task_id, payload, len(records))
            except BaseException:
                for download_task in download_tasks:
                    if not download_task.done():
                        download_task.cancel()
                await asyncio.gather(*download_tasks, return_exceptions=True)
                raise

            payload["phase"] = "organizing"
            self._set_status(task_id, "organizing", payload)
            # Reconcile against the channel-wide index so realtime and history
            # overlap safely. Existing paths remain immutable because Immich
            # identifies external assets by originalPath.
            channel_records = self._all_channel_records(chat_id)
            result = self._build_library(
                channel_records,
                library_dir,
                grouping_mode=payload.get("grouping_mode", "telegram_album"),
                marker_text=payload.get("marker_text", "1"),
                advertisement_policy=payload.get(
                    "advertisement_policy", "quarantine"
                ),
                display_spacing_hours=int(
                    payload.get("display_spacing_hours", 24)
                ),
                timeline_mode=payload.get("timeline_mode", "album"),
                generate_xmp=bool(payload.get("generate_xmp", True)),
                # A history task can overlap media imported by realtime
                # listening.  Always rewrite sidecars from the final,
                # channel-wide ordering before asking Immich to refresh.
                refresh_xmp=True,
                # 历史补全完成时整理不传 force_normalize：每次补全都强制重排会
                # 导致大量 Immich originalPath 失效重导，监听已下载部分也会被
                # 频繁移动。孤立硬链接由日常监听逐步清除；只有明确的排序错误才
                # 点击"安全校正排序"按钮，那里传 force_normalize=True。
            )
            task_result = self._count_batches(
                records,
                grouping_mode=payload.get("grouping_mode", "telegram_album"),
                marker_text=payload.get("marker_text", "1"),
                advertisement_policy=payload.get(
                    "advertisement_policy", "quarantine"
                ),
            )
            result = {
                "library_groups_total": result["groups"],
                "library_subjects_total": result["subjects"],
                "library_advertisements_total": result["advertisements"],
                **task_result,
            }
            payload.update(result)
            payload["task_root"] = str(task_root)
            payload["raw_path"] = str(self.settings.storage.download_path)
            payload["library_path"] = str(library_dir)
            payload["eta_seconds"] = 0
            payload["speed_bps"] = 0
            payload["active_download_count"] = 0
            payload["active_downloads"] = []
            payload["pending"] = 0
            payload["phase"] = "completed"
            payload.pop("partial_new_message_ids", None)
            with self.session_factory() as db:
                task_row = db.get(Task, task_id)
                if task_row:
                    task_row.status = "completed"
                    task_row.progress = len(records)
                    task_row.payload_json = json.dumps(payload, ensure_ascii=False)
                    task_row.finished_at = datetime.now(timezone.utc)
                    db.commit()
            if self.immich is not None:
                self.immich.schedule_refresh(refresh_metadata=True)
        except asyncio.CancelledError:
            with self.session_factory() as db:
                task_row = db.get(Task, task_id)
                if task_row and task_row.status in {
                    "scanning",
                    "running",
                    "organizing",
                }:
                    task_row.status = "queued"
                    db.commit()
            raise
        except Exception as exc:
            logger.exception("历史重构任务 %s 失败", task_id)
            with self.session_factory() as db:
                task_row = db.get(Task, task_id)
                if task_row:
                    task_row.status = "failed"
                    task_row.error = str(exc)
                    task_row.finished_at = datetime.now(timezone.utc)
                    db.commit()

    async def _get_fresh_message(
        self,
        client,
        entity,
        message_id: int,
        task_id: int,
        payload: dict,
        *,
        max_attempts: int | None = None,
    ):
        """Fetch one current Message object and transparently respect FloodWait."""
        last_error: Exception | None = None
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._respect_telegram_cooldown(task_id, payload)
                message = await client.get_messages(entity, ids=message_id)
                if not message or not getattr(message, "media", None):
                    raise RuntimeError(f"消息 {message_id} 已不存在或不再包含媒体")
                self._clear_retry_state(payload)
                return message
            except FloodWaitError as exc:
                last_error = exc
                wait_seconds = max(1, int(exc.seconds)) + 1
                await self._wait_for_telegram(
                    task_id,
                    payload,
                    wait_seconds,
                    f"Telegram 频率限制，等待后刷新消息 {message_id}",
                )
            except TRANSIENT_TELEGRAM_ERRORS as exc:
                last_error = exc
                if max_attempts is not None and attempt >= max_attempts:
                    break
                wait_seconds = self._network_retry_delay(attempt)
                await self._wait_for_retry(
                    task_id,
                    payload,
                    wait_seconds,
                    f"读取消息 {message_id} 暂时失败，正在自动重试",
                    attempt,
                )
                await self._recover_telegram_connection(client)
        raise RuntimeError(
            f"消息 {message_id} 刷新失败，已自动重试 {max_attempts} 次："
            f"{last_error}"
        )

    async def _download_with_retry(
        self,
        *,
        client,
        entity,
        message,
        partial: Path,
        progress_callback,
        task_id: int,
        payload: dict,
        max_attempts: int | None = None,
    ):
        """Download with a fresh Telegram reference, FloodWait and network retry."""
        message_id = int(message.id)
        last_error: Exception | None = None
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._respect_telegram_cooldown(task_id, payload)
                if partial.exists():
                    partial.unlink()
                payload["phase"] = "downloading"
                payload["retry_attempt"] = max(0, attempt - 1)
                payload["retry_reason"] = None
                payload["cooldown_until"] = None
                self._update_progress(task_id, payload, int(payload.get("processed", 0)))
                await message.download_media(
                    file=str(partial),
                    progress_callback=progress_callback,
                )
                if not partial.is_file():
                    raise RuntimeError(
                        f"消息 {message_id} 下载后未找到临时文件"
                    )
                expected_size = int(
                    getattr(getattr(message, "file", None), "size", 0) or 0
                )
                actual_size = partial.stat().st_size
                if expected_size and actual_size < expected_size:
                    raise OSError(
                        f"下载文件不完整：{actual_size}/{expected_size} 字节"
                    )
                self._clear_retry_state(payload)
                return message
            except FileReferenceExpiredError as exc:
                last_error = exc
                if partial.exists():
                    partial.unlink()
                if max_attempts is not None and attempt >= max_attempts:
                    break
                await self._wait_for_retry(
                    task_id,
                    payload,
                    min(2**attempt, 15),
                    f"消息 {message_id} 的临时文件引用已过期，正在刷新",
                    attempt,
                )
                message = await self._get_fresh_message(
                    client, entity, message_id, task_id, payload
                )
            except BadRequestError as exc:
                # Some Telegram DCs return FILE_REFERENCE_<n>_EXPIRED, which
                # Telethon 1.x may expose only as the generic BadRequestError.
                if "file reference" not in str(exc).lower():
                    raise
                last_error = exc
                if partial.exists():
                    partial.unlink()
                if max_attempts is not None and attempt >= max_attempts:
                    break
                await self._wait_for_retry(
                    task_id,
                    payload,
                    min(2**attempt, 15),
                    f"消息 {message_id} 的临时文件引用已失效，正在重新读取",
                    attempt,
                )
                message = await self._get_fresh_message(
                    client, entity, message_id, task_id, payload
                )
            except FloodWaitError as exc:
                last_error = exc
                if partial.exists():
                    partial.unlink()
                wait_seconds = max(1, int(exc.seconds)) + 1
                await self._wait_for_telegram(
                    task_id,
                    payload,
                    wait_seconds,
                    f"Telegram 对消息 {message_id} 限速，冷却后自动继续",
                )
                message = await self._get_fresh_message(
                    client, entity, message_id, task_id, payload
                )
            except TRANSIENT_TELEGRAM_ERRORS as exc:
                last_error = exc
                if partial.exists():
                    partial.unlink()
                if max_attempts is not None and attempt >= max_attempts:
                    break
                wait_seconds = self._network_retry_delay(attempt)
                await self._wait_for_retry(
                    task_id,
                    payload,
                    wait_seconds,
                    f"消息 {message_id} 下载连接中断，正在自动重试",
                    attempt,
                )
                await self._recover_telegram_connection(client)
                message = await self._get_fresh_message(
                    client, entity, message_id, task_id, payload
                )
        raise RuntimeError(
            f"消息 {message_id} 下载失败，已自动重试 {max_attempts} 次："
            f"{last_error}"
        )

    async def _scan_message_ids_with_retry(
        self,
        *,
        client,
        entity,
        start_utc: datetime,
        end_exclusive_utc: datetime,
        task_id: int,
        payload: dict,
    ) -> list[int]:
        """Read the date range again after a recoverable connection drop."""
        attempt = 0
        while True:
            attempt += 1
            message_ids: list[int] = []
            try:
                await self._respect_telegram_cooldown(task_id, payload)
                async for message in client.iter_messages(
                    entity,
                    offset_date=end_exclusive_utc,
                    reverse=False,
                ):
                    message_date = message.date
                    if message_date < start_utc:
                        break
                    if message_date >= end_exclusive_utc or not message.media:
                        continue
                    message_ids.append(int(message.id))
                self._clear_retry_state(payload)
                return message_ids
            except FloodWaitError as exc:
                await self._wait_for_telegram(
                    task_id,
                    payload,
                    max(1, int(exc.seconds)) + 1,
                    "Telegram 正在限速，冷却后自动继续读取历史消息",
                )
            except TRANSIENT_TELEGRAM_ERRORS:
                await self._wait_for_retry(
                    task_id,
                    payload,
                    self._network_retry_delay(attempt),
                    "Telegram 或代理连接中断，正在自动重连并继续读取任务",
                    attempt,
                )
                await self._recover_telegram_connection(client)

    async def _ensure_client_with_retry(self, task_id: int, payload: dict):
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self.telegram._ensure_client()
            except TRANSIENT_TELEGRAM_ERRORS:
                await self._wait_for_retry(
                    task_id,
                    payload,
                    self._network_retry_delay(attempt),
                    "无法连接 Telegram 或代理，序影将持续自动重连",
                    attempt,
                )

    async def _get_entity_with_retry(
        self, client, chat_id: int, task_id: int, payload: dict
    ):
        attempt = 0
        while True:
            attempt += 1
            try:
                await self._respect_telegram_cooldown(task_id, payload)
                return await client.get_entity(chat_id)
            except FloodWaitError as exc:
                await self._wait_for_telegram(
                    task_id,
                    payload,
                    max(1, int(exc.seconds)) + 1,
                    "Telegram 正在限速，冷却后自动继续访问频道",
                )
            except TRANSIENT_TELEGRAM_ERRORS:
                await self._wait_for_retry(
                    task_id,
                    payload,
                    self._network_retry_delay(attempt),
                    "读取频道时连接中断，正在自动重连",
                    attempt,
                )
                await self._recover_telegram_connection(client)

    @staticmethod
    def _network_retry_delay(attempt: int) -> int:
        """Fast first recovery, then a capped backoff without abandoning the task."""
        return (3, 8, 15, 30, 60, 120)[min(max(attempt - 1, 0), 5)]

    async def _recover_telegram_connection(self, client) -> None:
        """Reconnect only when Telethon says the shared client is disconnected."""
        async with self._connection_recovery_lock:
            is_connected = getattr(client, "is_connected", None)
            connected = is_connected() if callable(is_connected) else True
            if connected:
                return
            connect = getattr(client, "connect", None)
            if callable(connect):
                await connect()

    async def _wait_for_telegram(
        self,
        task_id: int,
        payload: dict,
        seconds: int,
        reason: str,
    ) -> None:
        self._telegram_cooldown_until = max(
            self._telegram_cooldown_until,
            monotonic_time.time() + seconds,
        )
        payload["phase"] = "rate_limited"
        payload["retry_reason"] = reason
        payload["cooldown_until"] = int(self._telegram_cooldown_until)
        self._update_progress(task_id, payload, int(payload.get("processed", 0)))
        await self._respect_telegram_cooldown(task_id, payload)

    async def _respect_telegram_cooldown(
        self,
        task_id: int,
        payload: dict,
    ) -> None:
        remaining = self._telegram_cooldown_until - monotonic_time.time()
        if remaining <= 0:
            return
        payload["phase"] = "rate_limited"
        payload["cooldown_until"] = int(self._telegram_cooldown_until)
        payload["retry_reason"] = (
            payload.get("retry_reason")
            or "Telegram 正在限速冷却，所有下载线程已暂停"
        )
        self._update_progress(task_id, payload, int(payload.get("processed", 0)))
        await asyncio.sleep(remaining)

    async def _wait_for_retry(
        self,
        task_id: int,
        payload: dict,
        seconds: int,
        reason: str,
        attempt: int,
    ) -> None:
        payload["phase"] = "retrying"
        payload["retry_attempt"] = attempt
        payload["retry_reason"] = reason
        payload["cooldown_until"] = int(monotonic_time.time()) + seconds
        self._update_progress(task_id, payload, int(payload.get("processed", 0)))
        await asyncio.sleep(seconds)

    def _clear_retry_state(self, payload: dict) -> None:
        if self._telegram_cooldown_until > monotonic_time.time():
            return
        payload["phase"] = "downloading"
        payload["retry_attempt"] = 0
        payload["retry_reason"] = None
        payload["cooldown_until"] = None

    def _build_library(
        self,
        records: list[dict],
        library_dir: Path,
        *,
        grouping_mode: str = "telegram_album",
        marker_text: str = "1",
        advertisement_policy: str = "quarantine",
        display_spacing_hours: int = 24,
        timeline_mode: str = "album",
        generate_xmp: bool = True,
        refresh_xmp: bool = False,
        migrate_generated_paths: bool = True,
        force_normalize: bool = False,
    ) -> dict[str, int]:
        channel_config = next(
            (
                item
                for item in self.settings.telegram.channels
                if records
                and records[0].get("chat_id") is not None
                and item.chat_id == int(records[0]["chat_id"])
            ),
            None,
        )
        keywords = (
            channel_config.advertisement_keywords
            if channel_config
            else ChannelConfig(name="default", chat_id=0).advertisement_keywords
        )
        batches = segment_records(
            records,
            grouping_mode=grouping_mode,
            marker_text=marker_text,
            advertisement_keywords=keywords,
        )
        dated_records = [
            datetime.fromisoformat(item["telegram_date"])
            for item in records
            if item.get("telegram_date")
        ]
        anchor = max(dated_records) if dated_records else datetime.now(timezone.utc)
        if timeline_mode == "spaced":
            timeline = display_datetimes(
                batches,
                anchor=anchor,
                spacing_hours=display_spacing_hours,
            )
        else:
            timeline = {
                id(batch): max(
                    (
                        datetime.fromisoformat(item["telegram_date"])
                        for item in batch.records
                        if item.get("telegram_date")
                    ),
                    default=anchor,
                )
                for batch in batches
            }
        counters = {
            "subject": 0,
            "advertisement": 0,
            "telegram_group": 0,
            "unassigned": 0,
        }
        sequence_manifest: list[dict] = []
        stale_generated_links: set[Path] = set()
        # Number media globally within each subject directory.  A subject
        # may span several Telegram batches; numbering per batch creates
        # duplicate 0002/0003 names in Immich.
        subject_counters: dict[Path, int] = {}
        subject_dirs_cleaned: set[Path] = set()
        subject_batches_seen: set[Path] = set()
        existing_duplicate_subject_dirs: set[Path] = set()
        subject_root = library_dir / "subjects"
        if subject_root.exists():
            for directory in (path for path in subject_root.rglob("*") if path.is_dir()):
                numbers = sorted(
                    int(match.group(1))
                    for item in directory.iterdir()
                    if item.is_file()
                    and not item.name.endswith(".xmp")
                    for match in [re.match(r"^(\d{4})_", item.name)]
                    if match
                )
                if numbers and numbers != list(range(1, len(numbers) + 1)):
                    # Covers both duplicate prefixes and gaps such as
                    # 0001, 0003, 0006 produced by older builds.
                    existing_duplicate_subject_dirs.add(directory)
        for batch in batches:
            counters[batch.kind] += 1
            if batch.kind == "advertisement" and advertisement_policy != "quarantine":
                continue
            group_records = batch.records
            start_id = min(int(item["message_id"]) for item in group_records)
            end_id = max(int(item["message_id"]) for item in group_records)
            prefixes = {
                "subject": ("subjects", "S"),
                "advertisement": ("advertisements", "A"),
                "telegram_group": ("groups", "G"),
                "unassigned": ("_unassigned", "U"),
            }
            parent_name, prefix = prefixes[batch.kind]
            stable_id = (
                batch.marker_message_id
                if batch.kind == "subject" and batch.marker_message_id
                else start_id
            )
            media_group_identity = next(
                (
                    str(item["media_group_id"])[-12:]
                    for item in group_records
                    if item.get("media_group_id")
                ),
                str(stable_id),
            )
            identity = (
                str(stable_id)
                if batch.kind == "subject"
                else media_group_identity
            )
            public_id = (
                f"{prefix}{abs(int(group_records[0].get('chat_id', 0))) % 1_000_000:06d}_"
                f"{identity}"
            )[:32]
            group_dir = assert_within(
                library_dir
                / parent_name
                / public_id,
                library_dir,
            )
            group_dir.mkdir(parents=True, exist_ok=True)
            sortable: list[RebuildMedia] = []
            for record in group_records:
                metadata = parse_filename(record["original_filename"])
                sortable.append(
                    RebuildMedia(
                        id=int(record["message_id"]),
                        original_filename=record["original_filename"],
                        saved_filename=record["saved_filename"],
                        source_path=record["source_path"],
                        original_order=int(record["message_id"]),
                        camera_prefix=metadata.camera_prefix,
                        camera_index=metadata.camera_index,
                    )
                )
            sorted_items = stable_camera_sort(sortable)
            # 封面消息需固定在第一个硬链接位置，确保缩略图编号稳定。
            if batch.kind == "subject" and batch.marker_message_id:
                marker_id = int(batch.marker_message_id)
                sorted_items = sorted(
                    sorted_items,
                    key=lambda item: 0 if int(item.id) == marker_id else 1,
                )
            normalize_subject = False
            subject_base = 0
            if batch.kind == "subject":
                # Normalize only when an old directory is already malformed
                # or when more than one logical batch maps to the same
                # directory.  This keeps valid Immich original paths stable.
                normalize_subject = (
                    force_normalize
                    or group_dir in existing_duplicate_subject_dirs
                    or group_dir in subject_batches_seen
                )
                subject_batches_seen.add(group_dir)
                subject_base = subject_counters.get(group_dir, 0)
                # 清理当前人物目录下编号开头的旧硬链接（补全中断或手动删除源文件后
                # 留下的孤立硬链接会导致编号跳号）；st_nlink < 2 时保留（唯一副本，
                # 源文件已删）；仅在执行编号重排时一次性清理，避免频繁 unlink。
                if normalize_subject and group_dir not in subject_dirs_cleaned:
                    subject_dirs_cleaned.add(group_dir)
                    for orphan in group_dir.iterdir():
                        if (
                            orphan.is_file()
                            and re.match(r"^\d{4}_", orphan.name)
                            and not orphan.name.endswith(".xmp")
                        ):
                            try:
                                if orphan.stat().st_nlink < 2:
                                    continue  # Preserve sole copies
                                orphan.unlink(missing_ok=True)
                                orphan.with_name(orphan.name + ".xmp").unlink(
                                    missing_ok=True
                                )
                            except OSError:
                                logger.warning("无法清理旧编号硬链接：%s", orphan)
            for batch_order, item in enumerate(sorted_items, start=1):
                source_path = Path(item.source_path)
                try:
                    source = assert_media_source(source_path, self.settings)
                except OrganizerError:
                    # 仅用于恢复 alpha.5-alpha.7 已经开始、但尚未完成的旧任务。
                    source = assert_within(source_path, self.settings.storage.rebuild_path)
                if batch.kind == "subject":
                    display_order = (
                        subject_base + batch_order
                        if normalize_subject
                        else batch_order
                    )
                else:
                    display_order = batch_order
                proposed_target = assert_within(
                    group_dir / f"{display_order:04d}_{item.saved_filename}",
                    library_dir,
                )
                existing_targets = self._find_existing_library_links(
                    library_dir, int(item.id), source
                )
                if migrate_generated_paths and batch.kind == "subject":
                    # alpha.29 and earlier realtime groups used an extra S in
                    # the folder identity (Sxxxx_S40466).  Always materialize
                    # the canonical channel-wide path first, then remove only
                    # the obsolete generated hardlinks after the full rebuild.
                    canonical_existing = [
                        candidate
                        for candidate in existing_targets
                        if candidate.parent == group_dir
                    ]
                    # Always use the global subject sequence.  Old generated
                    # hardlinks with duplicate per-batch prefixes are removed
                    # and recreated from the untouched raw source.
                    target = (
                        proposed_target
                        if normalize_subject
                        else (canonical_existing[0] if canonical_existing else proposed_target)
                    )
                    if normalize_subject:
                        for candidate in canonical_existing:
                            if candidate != target:
                                try:
                                    candidate.unlink(missing_ok=True)
                                    candidate.with_name(candidate.name + ".xmp").unlink(
                                        missing_ok=True
                                    )
                                except OSError:
                                    logger.warning("无法清理旧人物序号链接：%s", candidate)
                    stale_generated_links.update(
                        candidate
                        for candidate in existing_targets
                        if candidate.parent != group_dir
                    )
                else:
                    target = (
                        existing_targets[0]
                        if existing_targets
                        else proposed_target
                    )
                display_time = None
                if generate_xmp and batch.kind != "advertisement":
                    batch_time = timeline[id(batch)]
                    # Immich 时间线为最新在前，因此组内第一项使用稍新的秒数。
                    display_time = batch_time - timedelta(seconds=display_order - 1)
                if target.exists():
                    if os.path.samefile(source, target):
                        sidecar = target.with_name(target.name + ".xmp")
                        if display_time is not None and (
                            refresh_xmp or not sidecar.exists()
                        ):
                            write_xmp_sidecar(target, display_time)
                        continue
                    if (
                        batch.kind == "subject"
                        and migrate_generated_paths
                        and normalize_subject
                    ):
                        target.unlink(missing_ok=True)
                        target.with_name(target.name + ".xmp").unlink(
                            missing_ok=True
                        )
                    else:
                        raise OrganizerError(f"重构目标已存在且内容不同：{target}")
                os.link(source, target)
                if display_time is not None:
                    write_xmp_sidecar(target, display_time)
            if batch.kind == "subject":
                subject_counters[group_dir] = subject_base + len(sorted_items)
            sequence_manifest.append(
                {
                    "kind": batch.kind,
                    "stable_id": int(stable_id),
                    "public_id": public_id,
                    "start_message_id": start_id,
                    "end_message_id": end_id,
                    "marker_message_id": batch.marker_message_id,
                    "reason": batch.reason,
                    "confidence": batch.confidence,
                    "display_time": (
                        timeline[id(batch)].isoformat()
                        if id(batch) in timeline
                        else None
                    ),
                    "output_path": str(group_dir),
                }
            )
        for stale in sorted(stale_generated_links):
            stale = assert_within(stale, library_dir)
            try:
                stale.unlink(missing_ok=True)
                stale.with_name(stale.name + ".xmp").unlink(missing_ok=True)
            except OSError:
                logger.warning("无法清理旧版重复人物硬链接：%s", stale)

        sequence_path = library_dir / "sequence.json"
        sequence_path.write_text(
            json.dumps(
                sorted(
                    sequence_manifest,
                    key=lambda item: item["start_message_id"],
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._prune_empty_library_directories(library_dir)
        return {
            "groups": len(sequence_manifest),
            "subjects": counters["subject"],
            "advertisements": counters["advertisement"],
        }

    def _count_batches(
        self,
        records: list[dict],
        *,
        grouping_mode: str,
        marker_text: str,
        advertisement_policy: str,
    ) -> dict[str, int]:
        """Return counts for this task range instead of channel-wide totals."""
        if not records:
            return {"groups": 0, "subjects": 0, "advertisements": 0}
        chat_id = int(records[0].get("chat_id", 0))
        channel_config = next(
            (
                item
                for item in self.settings.telegram.channels
                if int(item.chat_id) == chat_id
            ),
            None,
        )
        keywords = (
            channel_config.advertisement_keywords
            if channel_config
            else ChannelConfig(name="default", chat_id=0).advertisement_keywords
        )
        batches = segment_records(
            records,
            grouping_mode=grouping_mode,
            marker_text=marker_text,
            advertisement_keywords=keywords,
        )
        visible = [
            batch
            for batch in batches
            if batch.kind != "advertisement"
            or advertisement_policy == "quarantine"
        ]
        return {
            "groups": len(visible),
            "subjects": sum(batch.kind == "subject" for batch in visible),
            "advertisements": sum(
                batch.kind == "advertisement" for batch in visible
            ),
        }

    @staticmethod
    def _prune_empty_library_directories(library_dir: Path) -> None:
        """Remove empty generated folders after messages move between subjects."""
        generated_roots = (
            library_dir / "subjects",
            library_dir / "groups",
            library_dir / "advertisements",
            library_dir / "_unassigned",
        )
        for root in generated_roots:
            if not root.exists():
                continue
            for candidate in sorted(
                (path for path in root.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    candidate.rmdir()
                except OSError:
                    pass

    def _update_progress(self, task_id: int, payload: dict, progress: int) -> None:
        with self.session_factory() as db:
            task_row = db.get(Task, task_id)
            if task_row:
                task_row.progress = progress
                task_row.payload_json = json.dumps(payload, ensure_ascii=False)
                db.commit()

    def schedule_channel_reconcile(
        self,
        chat_id: int,
        delay: float = 3.0,
        refresh_metadata: bool = False,
    ) -> None:
        """Debounce live updates into one channel-wide subject rebuild."""
        chat_id = int(chat_id)
        self._reconcile_refresh[chat_id] = (
            self._reconcile_refresh.get(chat_id, False)
            or bool(refresh_metadata)
        )
        previous = self._reconcile_tasks.get(chat_id)
        if previous and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            self._reconcile_channel_after_delay(chat_id, delay)
        )
        self._reconcile_tasks[chat_id] = task
        task.add_done_callback(
            lambda completed, current_chat_id=chat_id: (
                self._reconcile_tasks.pop(current_chat_id, None)
                if self._reconcile_tasks.get(current_chat_id) is completed
                else None
            )
        )

    async def _reconcile_channel_after_delay(
        self, chat_id: int, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            # An older, disjoint history task must not block today's live person.
            # Wait only when Telegram message identity touches the open subject.
            while self._active_history_conflicts_with_live(chat_id):
                await asyncio.sleep(3)
            refresh_metadata = self._reconcile_refresh.get(chat_id, False)
            await self.reconcile_channel(
                chat_id,
                refresh_xmp=refresh_metadata,
                force_generate_xmp=refresh_metadata,
                exclude_message_ids=self._active_history_partial_ids(chat_id),
            )
            self._reconcile_refresh.pop(chat_id, None)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("频道 %s 统一人物批次整理失败", chat_id)

    def _has_active_history_task(self, chat_id: int) -> bool:
        with self.session_factory() as db:
            rows = db.scalars(
                select(Task).where(
                    Task.kind == "history_rebuild",
                    Task.status.in_(
                        ["queued", "scanning", "running", "organizing"]
                    ),
                )
            )
            for row in rows:
                payload = json.loads(row.payload_json or "{}")
                if int(payload.get("chat_id", 0)) == int(chat_id):
                    return True
        return False

    def _active_history_partial_ids(self, chat_id: int) -> set[int]:
        """Hide incomplete history media from live album reconciliation."""
        result: set[int] = set()
        with self.session_factory() as db:
            rows = db.scalars(
                select(Task).where(
                    Task.kind == "history_rebuild",
                    Task.status.in_(
                        ["queued", "scanning", "running", "organizing"]
                    ),
                )
            )
            for row in rows:
                payload = json.loads(row.payload_json or "{}")
                if int(payload.get("chat_id", 0)) != int(chat_id):
                    continue
                result.update(
                    int(message_id)
                    for message_id in payload.get("partial_new_message_ids", [])
                )
        return result

    def _active_history_conflicts_with_live(self, chat_id: int) -> bool:
        """Use Telegram content identity, not dates, to decide coordination."""
        chat_id = int(chat_id)
        channel_config = next(
            (
                channel
                for channel in self.settings.telegram.channels
                if int(channel.chat_id) == chat_id
            ),
            None,
        )
        if channel_config is None:
            return False
        with self.session_factory() as db:
            tasks = list(
                db.scalars(
                    select(Task).where(
                        Task.kind == "history_rebuild",
                        Task.status.in_(
                            ["queued", "scanning", "running", "organizing"]
                        ),
                    )
                )
            )
            ranges: list[tuple[int, int]] = []
            for task in tasks:
                payload = json.loads(task.payload_json or "{}")
                if int(payload.get("chat_id", 0)) != chat_id:
                    continue
                lower = payload.get("message_id_min")
                upper = payload.get("message_id_max")
                if lower is None or upper is None:
                    # Scanning is normally brief. Wait for message IDs rather
                    # than guessing whether two date selections overlap.
                    if task.status in {"queued", "scanning"}:
                        return True
                    continue
                ranges.append((int(lower), int(upper)))
            if not ranges:
                return False

            if channel_config.grouping_mode == "marker":
                current = db.scalar(
                    select(MediaGroup)
                    .where(
                        MediaGroup.chat_id == chat_id,
                        MediaGroup.reason == "channel_marker",
                        MediaGroup.status.in_(["open", "pending"]),
                    )
                    .order_by(MediaGroup.start_message_id.desc())
                )
                if current is None:
                    return False
                return any(
                    upper >= int(current.start_message_id)
                    for _lower, upper in ranges
                )

            live_groups = list(
                db.scalars(
                    select(MediaGroup).where(
                        MediaGroup.chat_id == chat_id,
                        MediaGroup.status.in_(["open", "pending", "organizing"]),
                    )
                )
            )
            return any(
                lower <= int(group.end_message_id)
                and upper >= int(group.start_message_id)
                for lower, upper in ranges
                for group in live_groups
            )

    async def reconcile_channel(
        self,
        chat_id: int,
        *,
        refresh_xmp: bool = False,
        force_generate_xmp: bool = False,
        exclude_message_ids: set[int] | None = None,
    ) -> dict[str, int]:
        """Recalculate one canonical subject layout from every known message."""
        chat_id = int(chat_id)
        lock = self._channel_reconcile_locks.setdefault(
            chat_id, asyncio.Lock()
        )
        async with lock:
            records = self._all_channel_records(
                chat_id, exclude_message_ids=exclude_message_ids
            )
            if not records:
                return {"groups": 0, "subjects": 0, "advertisements": 0}
            channel_config = next(
                (
                    item
                    for item in self.settings.telegram.channels
                    if int(item.chat_id) == chat_id
                ),
                ChannelConfig(
                    name=str(chat_id),
                    chat_id=chat_id,
                    grouping_mode="marker",
                ),
            )
            library_dir = assert_within(
                self.settings.storage.library_path
                / safe_segment(str(chat_id), "unknown-channel"),
                self.settings.storage.library_path,
            )
            library_dir.mkdir(parents=True, exist_ok=True)
            result = self._build_library(
                records,
                library_dir,
                grouping_mode=channel_config.grouping_mode,
                marker_text=channel_config.marker_text,
                advertisement_policy=channel_config.advertisement_policy,
                display_spacing_hours=channel_config.display_spacing_hours,
                timeline_mode=channel_config.timeline_mode,
                generate_xmp=(
                    force_generate_xmp
                    or self.settings.organizer.generate_xmp
                ),
                refresh_xmp=refresh_xmp,
            )
            with self.session_factory() as db:
                marker_groups = list(
                    db.scalars(
                        select(MediaGroup).where(
                            MediaGroup.chat_id == chat_id,
                            MediaGroup.reason == "channel_marker",
                        )
                    )
                )
                channel_token = abs(chat_id) % 1_000_000
                for group in marker_groups:
                    suffix = group.public_id.split("_", 1)[-1]
                    if suffix.startswith("S") and suffix[1:].isdigit():
                        canonical_id = f"S{channel_token:06d}_{suffix[1:]}"[:32]
                        collision = db.scalar(
                            select(MediaGroup.id).where(
                                MediaGroup.public_id == canonical_id,
                                MediaGroup.id != group.id,
                            )
                        )
                        if not collision:
                            group.public_id = canonical_id
                    if group.status in {"pending", "organizing", "error"}:
                        group.status = "organized"
                        group.output_path = str(
                            library_dir
                            / "subjects"
                            / f"S{channel_token:06d}_{group.start_message_id}"
                        )
                db.commit()
            if self.immich is not None:
                self.immich.schedule_refresh(refresh_metadata=refresh_xmp)
            return result

    async def repair_channel_order(self, chat_id: int) -> dict:
        """Safely repair every completed media item for a listening channel."""
        chat_id = int(chat_id)
        if self._has_active_history_task(chat_id):
            return {
                "chat_id": chat_id,
                "status": "deferred",
                "message": (
                    "该频道正在历史补全；补全完成时会自动使用最新规则统一整理，"
                    "现在无需暂停监听或重复操作。"
                ),
            }
        raw_cover_renamed = self._rename_raw_cover_markers(chat_id)
        result = await self.reconcile_channel(
            chat_id,
            refresh_xmp=True,
            force_generate_xmp=True,
        )
        # 监听频道的修复按钮不传 force_normalize，保留 Immich 路径稳定。
        # 历史补全完成时已调用 force_normalize=True 重排了一次。
        return {
            "chat_id": chat_id,
            "status": "completed",
            "message": (
                "频道排序已安全校正；没有重新下载或移动 Immich 媒体路径。"
                "已安排扫描和人物相册同步。"
            ),
            "raw_cover_renamed": raw_cover_renamed,
            **result,
        }

    def _rename_raw_cover_markers(self, chat_id: int) -> int:
        """Normalize exact marker media to one visible cover tag."""
        chat_id = int(chat_id)
        channel_config = next(
            (item for item in self.settings.telegram.channels if int(item.chat_id) == chat_id),
            None,
        )
        marker_text = channel_config.marker_text if channel_config else "1"
        renamed = 0
        seen_paths: set[str] = set()
        with self.session_factory() as db:
            rebuild_rows = list(
                db.scalars(select(RebuildItem).where(RebuildItem.chat_id == chat_id))
            )
            live_rows = list(
                db.execute(
                    select(TelegramMessage, MediaFile)
                    .join(MediaFile, MediaFile.message_id_fk == TelegramMessage.id)
                    .where(TelegramMessage.chat_id == chat_id)
                )
            )
            candidates: list[tuple[Path, str, object, int]] = []
            for row in rebuild_rows:
                if is_marker_caption(row.caption, marker_text):
                    candidates.append((Path(row.source_path), row.original_filename, row, int(row.message_id)))
            for message, media in live_rows:
                if is_marker_caption(message.caption, marker_text):
                    candidates.append((Path(media.original_path), media.original_filename, media, int(message.message_id)))

            for source, original_name, row, message_id in candidates:
                if str(source) in seen_paths or not source.is_file():
                    continue
                seen_paths.add(str(source))
                target_name = f"{message_id} - \u005b\u5c01\u9762\u005d {self.telegram._safe_filename(original_name)}"
                target = source.with_name(target_name)
                if source != target:
                    if target.exists():
                        logger.warning("Raw cover target already exists; source kept: %s", source)
                        continue
                    try:
                        source.rename(target)
                    except OSError:
                        logger.warning("Unable to annotate raw cover: %s", source)
                        continue
                    renamed += 1
                if isinstance(row, RebuildItem):
                    row.source_path = str(target)
                    row.saved_filename = target_name
                else:
                    row.original_path = str(target)
                    row.saved_filename = target_name
            db.commit()
        return renamed

    def _all_channel_records(
        self,
        chat_id: int,
        *,
        exclude_message_ids: set[int] | None = None,
    ) -> list[dict]:
        """Return a de-duplicated union of history and realtime media."""
        records: dict[int, dict] = {}
        excluded = {int(value) for value in (exclude_message_ids or set())}
        with self.session_factory() as db:
            for row in db.scalars(
                select(RebuildItem)
                .where(RebuildItem.chat_id == int(chat_id))
                .order_by(RebuildItem.message_id)
            ):
                if int(row.message_id) in excluded:
                    continue
                if Path(row.source_path).is_file():
                    record = self._index_to_record(row)
                    record["owner"] = "rebuild"
                    records[int(row.message_id)] = record
            live_rows = db.execute(
                select(TelegramMessage, MediaFile)
                .join(MediaFile, MediaFile.message_id_fk == TelegramMessage.id)
                .where(TelegramMessage.chat_id == int(chat_id))
                .order_by(TelegramMessage.message_id)
            )
            for message, media in live_rows:
                if not Path(media.original_path).is_file():
                    continue
                records[int(message.message_id)] = {
                    "chat_id": int(message.chat_id),
                    "message_id": int(message.message_id),
                    "media_group_id": message.media_group_id,
                    "telegram_date": (
                        message.telegram_date.isoformat()
                        if message.telegram_date
                        else None
                    ),
                    "caption": message.caption,
                    "original_filename": media.original_filename,
                    "saved_filename": media.saved_filename,
                    "source_path": media.original_path,
                    "size_bytes": int(media.size_bytes or 0),
                    "owner": "live",
                }
        return [records[key] for key in sorted(records)]

    def _set_status(self, task_id: int, status: str, payload: dict) -> None:
        with self.session_factory() as db:
            task_row = db.get(Task, task_id)
            if task_row:
                task_row.status = status
                task_row.payload_json = json.dumps(payload, ensure_ascii=False)
                db.commit()

    def _get_index_record(self, chat_id: int, message_id: int) -> dict | None:
        with self.session_factory() as db:
            live = db.execute(
                select(TelegramMessage, MediaFile)
                .join(MediaFile, MediaFile.message_id_fk == TelegramMessage.id)
                .where(
                    TelegramMessage.chat_id == chat_id,
                    TelegramMessage.message_id == message_id,
                )
            ).first()
            if live:
                telegram_message, media_file = live
                record = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "media_group_id": telegram_message.media_group_id,
                    "telegram_date": (
                        telegram_message.telegram_date.isoformat()
                        if telegram_message.telegram_date
                        else None
                    ),
                    "caption": telegram_message.caption,
                    "original_filename": media_file.original_filename,
                    "saved_filename": media_file.saved_filename,
                    "source_path": media_file.original_path,
                    "size_bytes": int(media_file.size_bytes or 0),
                    "owner": "live",
                }
                if Path(record["source_path"]).is_file():
                    self._store_index(record)
                    return record
            row = db.scalar(
                select(RebuildItem).where(
                    RebuildItem.chat_id == chat_id,
                    RebuildItem.message_id == message_id,
                )
            )
            if row and Path(row.source_path).is_file():
                record = self._index_to_record(row)
                record["owner"] = "rebuild"
                return record
        return None

    def _store_index(self, record: dict) -> None:
        with self.session_factory() as db:
            row = db.scalar(
                select(RebuildItem).where(
                    RebuildItem.chat_id == int(record["chat_id"]),
                    RebuildItem.message_id == int(record["message_id"]),
                )
            )
            telegram_date = record.get("telegram_date")
            parsed_date = (
                datetime.fromisoformat(telegram_date)
                if isinstance(telegram_date, str) and telegram_date
                else telegram_date
            )
            values = {
                "media_group_id": record.get("media_group_id"),
                "telegram_date": parsed_date,
                "caption": record.get("caption"),
                "original_filename": record["original_filename"],
                "saved_filename": record["saved_filename"],
                "source_path": record["source_path"],
                "size_bytes": int(record.get("size_bytes") or 0),
            }
            if row:
                for key, value in values.items():
                    setattr(row, key, value)
            else:
                row = RebuildItem(
                    chat_id=int(record["chat_id"]),
                    message_id=int(record["message_id"]),
                    **values,
                )
                db.add(row)
            db.commit()

    @staticmethod
    def _index_to_record(row: RebuildItem) -> dict:
        return {
            "chat_id": row.chat_id,
            "message_id": row.message_id,
            "media_group_id": row.media_group_id,
            "telegram_date": (
                row.telegram_date.isoformat() if row.telegram_date else None
            ),
            "caption": row.caption,
            "original_filename": row.original_filename,
            "saved_filename": row.saved_filename,
            "source_path": row.source_path,
            "size_bytes": row.size_bytes,
        }

    def _backfill_global_index(self) -> None:
        """Import alpha.8 manifests so the first alpha.9 task also deduplicates."""
        root = self.settings.storage.rebuild_path
        if not root.exists():
            return
        for manifest in root.rglob("manifest.jsonl"):
            try:
                for record in self._load_manifest(manifest):
                    if (
                        record.get("chat_id") is not None
                        and record.get("message_id") is not None
                        and Path(record.get("source_path", "")).is_file()
                    ):
                        self._store_index(record)
            except Exception:
                logger.warning("跳过无法导入的旧任务清单：%s", manifest)

    @staticmethod
    def _find_existing_library_links(
        library_dir: Path, message_id: int, source: Path
    ) -> list[Path]:
        """Find generated hardlinks for one Telegram message within the library."""
        pattern = f"*_{message_id} -*"
        matches: list[Path] = []
        for candidate in sorted(library_dir.rglob(pattern)):
            if not candidate.is_file() or candidate.name.endswith(".xmp"):
                continue
            try:
                if os.path.samefile(source, candidate):
                    matches.append(candidate)
            except OSError:
                continue
        return matches

    @staticmethod
    def _load_manifest(path: Path) -> list[dict]:
        if not path.exists():
            return []
        records: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    @staticmethod
    def _append_manifest(path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _serialize(task: Task) -> dict:
        payload = json.loads(task.payload_json or "{}")
        total = int(payload.get("total") or 0)
        progress = int(task.progress or 0)
        current_total = int(payload.get("current_file_total") or 0)
        current_received = int(payload.get("current_file_received") or 0)
        fractional = (
            min(1.0, current_received / current_total) if current_total else 0
        )
        return {
            "id": task.id,
            "status": task.status,
            "progress": task.progress,
            "error": task.error,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "total": total,
            "pending": max(0, total - progress) if total else payload.get("pending", 0),
            "percent": (
                round((progress + fractional) * 100 / total, 1) if total else 0
            ),
            **payload,
        }
