from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Literal

from app.config import Settings
from app.services.immich import ImmichClient

logger = logging.getLogger(__name__)
SyncTarget = Literal["main", "forwarded"]


@dataclass
class SyncStatus:
    state: str = "idle"
    attempt: int = 0
    missing_assets: int = 0
    created_albums: int = 0
    updated_albums: int = 0
    removed_album_assets: int = 0
    repaired_album_assets: int = 0
    album_member_missing_assets: int = 0
    album_member_assets: int = 0
    album_timeline_assets: int = 0
    album_count: int = 0
    collapsed_live_photo_assets: int = 0
    album_verification_errors: int = 0
    unsupported_assets: int = 0
    message: str = "等待下一次整理"


class ImmichSyncOrchestrator:
    """Run idempotent scans and album synchronization outside the API client."""

    def __init__(self, settings: Settings, client: ImmichClient):
        self.settings = settings
        self.client = client
        self._tasks: dict[SyncTarget, asyncio.Task | None] = {
            "main": None,
            "forwarded": None,
        }
        self._requested: dict[SyncTarget, bool] = {
            "main": False,
            "forwarded": False,
        }
        self._metadata_requested = False
        self._statuses: dict[SyncTarget, SyncStatus] = {
            "main": SyncStatus(),
            "forwarded": SyncStatus(message="等待机器人下载任务"),
        }
        self._report_path = (
            self.settings.storage.data_path / "immich-album-report.json"
        )
        self._report: dict = self._load_report()
        self._transcode_path = (
            self.settings.storage.data_path / "immich-motion-transcodes.json"
        )
        self._transcoded: set[str] = self._load_transcoded()

    def _remember_unmerged(self, result: dict) -> None:
        """Persist motion halves that have been un-merged.

        They must never be sent for metadata refresh again: extraction re-runs
        `linkLivePhotos`, which would pair and hide them a second time. Without
        this the merge returns on the next refresh and the counts oscillate.
        """
        ids = result.get("revealed_motion_ids")
        if not isinstance(ids, list):
            return
        fresh = {str(item) for item in ids if str(item).strip()}
        if fresh - self._transcoded:
            self._transcoded |= fresh
            self._save_transcoded()

    def _load_transcoded(self) -> set[str]:
        """Motion halves already un-merged and sent for transcode.

        Persisted so a restart does not refresh them back into a merged pair.
        """
        try:
            raw = json.loads(self._transcode_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(raw, list):
            return set()
        return {str(item) for item in raw if str(item).strip()}

    def _save_transcoded(self) -> None:
        try:
            self._transcode_path.parent.mkdir(parents=True, exist_ok=True)
            self._transcode_path.write_text(
                json.dumps(sorted(self._transcoded), ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("无法记录已补转码的 Live Photo 视频，下次会重复请求")

    async def _queue_motion_transcodes(self, result: dict) -> int:
        """Ask Immich to transcode motion halves it will never queue itself.

        Capped per pass: a history backfill can merge hundreds of Live Photos
        at once, and a NAS should not be handed all of them in one go. The rest
        carry over because only ids Immich accepted are marked as done.
        """
        ids = result.get("motion_asset_ids")
        if not isinstance(ids, list):
            return 0
        pending = [str(item) for item in ids if str(item) not in self._transcoded]
        if not pending:
            return 0
        batch = pending[:50]
        try:
            queued = await self.client.request_video_transcode(batch)
        except Exception:
            logger.warning("补齐 Live Photo 视频转码失败，下次同步再试")
            return 0
        if queued:
            self._transcoded.update(batch[:queued])
            self._save_transcoded()
        return queued

    def _load_report(self) -> dict:
        """Read the last per-album report so a restart keeps showing it."""
        try:
            raw = json.loads(self._report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"generated_at": "", "albums": []}
        if not isinstance(raw, dict):
            return {"generated_at": "", "albums": []}
        albums = raw.get("albums")
        return {
            "generated_at": str(raw.get("generated_at") or ""),
            "albums": albums if isinstance(albums, list) else [],
        }

    def _save_report(self, albums: list) -> None:
        """Persist the per-album report; a write failure must not stop sync."""
        self._report = {
            "generated_at": datetime.now(timezone.utc)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S"),
            "albums": albums,
        }
        try:
            self._report_path.parent.mkdir(parents=True, exist_ok=True)
            self._report_path.write_text(
                json.dumps(self._report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("无法写入人物相册明细报表，本次仅保留在内存中")

    def album_report(self) -> dict:
        return self._report

    async def start(self) -> None:
        """Recover unfinished synchronization after a container restart."""
        if self._enabled("main"):
            self.request("main")
        if self._enabled("forwarded"):
            self.request("forwarded")

    def _enabled(self, target: SyncTarget) -> bool:
        if not self.client.configured:
            return False
        config = self.settings.immich
        if target == "main":
            return bool(config.auto_scan and config.library_id)
        return bool(config.forwarded_auto_scan and config.forwarded_library_id)

    def schedule_refresh(self, *, refresh_metadata: bool = False) -> None:
        self.request("main", refresh_metadata=refresh_metadata)

    def schedule_forwarded_refresh(self) -> None:
        self.request("forwarded")

    def request(
        self,
        target: SyncTarget,
        *,
        refresh_metadata: bool = False,
        force: bool = False,
    ) -> bool:
        if not force and not self._enabled(target):
            return False
        if not self.client.configured:
            return False
        self._requested[target] = True
        if target == "main":
            self._metadata_requested = self._metadata_requested or refresh_metadata
        self._set_status(
            target,
            state="queued",
            attempt=0,
            message=(
                "已排队：将扫描频道人物库并校正相册"
                if target == "main"
                else "已排队：将扫描机器人下载库并同步相册"
            ),
        )
        task = self._tasks[target]
        if task is None or task.done():
            self._tasks[target] = asyncio.create_task(self._worker(target))
        return True

    def status(self) -> dict:
        main = asdict(self._statuses["main"])
        forwarded = asdict(self._statuses["forwarded"])
        return {**main, "main": main, "forwarded": forwarded}

    def _set_status(self, target: SyncTarget, **values) -> None:
        status = self._statuses[target]
        for key, value in values.items():
            setattr(status, key, value)

    async def _worker(self, target: SyncTarget) -> None:
        try:
            while self._requested[target]:
                self._requested[target] = False
                refresh_metadata = False
                if target == "main":
                    refresh_metadata = self._metadata_requested
                    self._metadata_requested = False
                await asyncio.sleep(10)
                await self._run_pipeline(target, refresh_metadata=refresh_metadata)
        except asyncio.CancelledError:
            return
        except Exception:
            self._set_status(
                target,
                state="retry_pending",
                message="自动同步暂时失败；下次整理会自动重试",
            )
            logger.exception("Immich %s synchronization failed", target)
            # Re-schedule so the next organizer event can trigger a fresh
            # attempt. Without this the worker exits and sync never resumes.
            await asyncio.sleep(60)
            if not self._requested[target]:
                self._requested[target] = True
            self._tasks[target] = asyncio.create_task(self._worker(target))

    async def _run_pipeline(
        self,
        target: SyncTarget,
        *,
        refresh_metadata: bool,
    ) -> None:
        self._set_status(
            target,
            state="scanning",
            message=(
                "正在通知 Immich 扫描频道人物库"
                if target == "main"
                else "正在通知 Immich 扫描机器人下载库"
            ),
        )
        if target == "main":
            await self.client.scan_library()
            should_sync = self.settings.immich.auto_album
        else:
            await self.client.scan_forwarded_library()
            should_sync = self.settings.immich.forwarded_auto_album
        if not should_sync:
            self._set_status(target, state="completed", message="外部库扫描已经提交")
            return

        await asyncio.sleep(30)
        refreshed_asset_ids: set[str] = set()
        max_attempts = 48 if target == "main" else 24
        last_missing: int | None = None
        stable_rounds = 0
        for attempt in range(1, max_attempts + 1):
            # Immich can accept a scan request before its previous scan has
            # indexed new sidecars. Re-submit periodically, not every poll.
            if attempt > 1 and attempt % 4 == 1:
                if target == "main":
                    await self.client.scan_library()
                else:
                    await self.client.scan_forwarded_library()
            self._set_status(
                target,
                state="syncing",
                attempt=attempt,
                message="正在等待 Immich 入库并同步相册",
            )
            if target == "main":
                result = await self.client.sync_subject_albums(
                    refresh_metadata=refresh_metadata,
                    metadata_refreshed_asset_ids=refreshed_asset_ids,
                    unmerged_motion_ids=self._transcoded,
                )
                self._remember_unmerged(result)
            else:
                result = await self.client.sync_forwarded_albums()
            if target == "main" and result.get("album_reports") is not None:
                # Persist on every pass, including one that still reports
                # missing media: a mid-import snapshot is what explains a
                # temporarily short album.
                self._save_report(list(result.get("album_reports") or []))
            if target == "main":
                await self._queue_motion_transcodes(result)
            missing = int(result.get("missing_assets", 0))
            self._set_status(
                target,
                missing_assets=missing,
                created_albums=int(result.get("created_albums", 0)),
                updated_albums=int(result.get("updated_albums", 0)),
                removed_album_assets=int(result.get("removed_album_assets", 0)),
                repaired_album_assets=int(result.get("repaired_album_assets", 0)),
                album_member_missing_assets=int(
                    result.get("album_member_missing_assets", 0)
                ),
                album_member_assets=int(result.get("album_member_assets", 0)),
                album_timeline_assets=int(
                    result.get("album_timeline_assets", 0)
                ),
                album_count=len(result.get("album_reports") or []),
                collapsed_live_photo_assets=int(
                    result.get("collapsed_live_photo_assets", 0)
                ),
                album_verification_errors=int(
                    result.get("album_verification_errors", 0)
                ),
                unsupported_assets=int(result.get("unsupported_assets", 0)),
            )
            unsupported = int(result.get("unsupported_assets", 0))
            if unsupported:
                # These files can never enter an album: Immich refuses the
                # extension outright. Say so instead of reporting success.
                examples = result.get("unsupported_examples") or []
                logger.warning(
                    "%d files in person directories use extensions Immich "
                    "cannot import; examples: %s",
                    unsupported,
                    list(examples)[:5],
                )
            if missing == 0:
                metadata_jobs = int(result.get("metadata_refresh_assets", 0))
                collapsed_live_photos = int(
                    result.get("collapsed_live_photo_assets", 0)
                )
                repaired_members = int(result.get("repaired_album_assets", 0))
                if target == "main" and unsupported:
                    completed_message = (
                        "频道人物库相册成员已核对完成；"
                        f"另有 {unsupported} 个文件的扩展名 Immich 无法入库"
                        "（多为无文件名的 Telegram 视频落成 .bin），"
                        "这些文件需要重新下载后才能进入相册"
                    )
                elif target == "main" and repaired_members:
                    completed_message = (
                        f"频道人物库已逐项核对并补回 {repaired_members} 个遗漏成员；"
                        "人物相册成员现在与硬链接人物目录一致"
                    )
                elif target == "main" and metadata_jobs:
                    completed_message = (
                        "频道人物库相册成员已逐项核对完成；"
                        "元数据刷新已提交，Immich 任务归零表示处理完毕"
                    )
                else:
                    completed_message = (
                        "频道人物库扫描和相册成员核对已经完成"
                        if target == "main"
                        else "机器人下载库扫描和相册同步已经完成"
                    )
                unpaired = int(result.get("unpaired_live_photos", 0))
                if target == "main" and unpaired:
                    completed_message += (
                        f"。本次把 {unpaired} 个被 Immich 合并为 Live Photo 的"
                        "视频部分取消隐藏，并补发了缩略图和转码：隐藏状态下这些"
                        "视频既拿不到封面也排不进转码队列，合并后的项目在网页和"
                        "手机端都放不了。取消隐藏后它们会作为独立视频出现在时间"
                        "线上，各自可以正常播放。缩略图和转码任务归零后刷新即可"
                    )
                elif target == "main" and collapsed_live_photos:
                    # Reaching here means unpairing was refused for some pairs;
                    # a timeline shorter than the directory is then expected.
                    reason = str(result.get("unpair_error") or "").strip()
                    completed_message += (
                        f"。仍有 {collapsed_live_photos} 个视频被 Immich 合并为"
                        "Live Photo 的视频部分，本次未能取消隐藏"
                    )
                    if reason:
                        completed_message += (
                            f"（{reason}；若是 403 请把 Immich API Key 权限"
                            "改为完全访问）"
                        )
                    completed_message += "，时间线项目数因此比人物目录少这些；下次同步会重试"
                self._set_status(
                    target,
                    state="completed",
                    message=completed_message,
                )
                return
            album_member_missing = int(
                result.get("album_member_missing_assets", 0)
            )
            verification_errors = int(
                result.get("album_verification_errors", 0)
            )
            if verification_errors:
                self._set_status(
                    target,
                    message=(
                        f"Immich 相册成员核验失败 {verification_errors} 个相册；"
                        "序影会自动重试，不会把整批媒体误报为未入库"
                    ),
                )
            elif album_member_missing:
                self._set_status(
                    target,
                    message=(
                        f"Immich 人物相册仍缺少 {album_member_missing} 个成员；"
                        "序影正在按硬链接人物目录逐项补回"
                    ),
                )
            else:
                self._set_status(
                    target,
                    message=(
                        f"Immich 尚待入库 {missing} 项；"
                        "入库完成后会自动逐项校正人物相册"
                    ),
                )
            if missing == last_missing:
                stable_rounds += 1
            else:
                stable_rounds = 0
                last_missing = missing
            if stable_rounds >= 8:
                if verification_errors:
                    waiting_message = (
                        f"Immich 相册成员核验连续失败 "
                        f"{verification_errors} 个相册；已暂停重复请求，"
                        "请查看序影容器日志中的 Immich HTTP 状态码"
                    )
                elif album_member_missing:
                    waiting_message = (
                        f"Immich 人物相册连续校正后仍缺少 "
                        f"{album_member_missing} 个成员；已停止无限重试"
                    )
                else:
                    waiting_message = (
                        f"Immich 连续多次未入库 {missing} 项，已停止重复校正；"
                        "请检查所选外部库及容器挂载路径后再同步"
                    )
                self._set_status(
                    target,
                    state="waiting",
                    message=waiting_message,
                )
                return
            await asyncio.sleep(150)
        self._set_status(
            target,
            state="waiting",
            message="仍有资源尚未入库；下次整理会继续校正",
        )

    async def sync_now(self, target: SyncTarget) -> dict:
        if target == "main":
            result = await self.client.sync_subject_albums(
                unmerged_motion_ids=self._transcoded
            )
            self._remember_unmerged(result)
            # This path skips the pipeline, so refresh the report here too;
            # otherwise the table keeps showing counts from an older pass.
            if result.get("album_reports") is not None:
                self._save_report(list(result.get("album_reports") or []))
            await self._queue_motion_transcodes(result)
            return result
        return await self.client.sync_forwarded_albums()

    async def stop(self) -> None:
        active = [
            task for task in self._tasks.values()
            if task is not None and not task.done()
        ]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
