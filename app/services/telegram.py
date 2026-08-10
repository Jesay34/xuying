from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
import weakref
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import socks
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker
from telethon import TelegramClient, events
from telethon.errors import (
    AuthKeyError,
    FileReferenceExpiredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    RpcCallFailError,
    ServerError,
    SessionPasswordNeededError,
    TimedOutError,
)
from telethon.tl.types import DocumentAttributeFilename
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

from app.config import (
    ChannelConfig,
    Settings,
    save_runtime_secrets,
    save_telegram_bot_config,
    save_telegram_runtime_config,
)
from app.models import (
    Channel,
    LiveDownloadItem,
    MediaFile,
    MediaGroup,
    RebuildItem,
    Task,
    TelegramMessage,
)
from app.services.content_rules import (
    is_advertisement_caption,
    is_marker_caption,
)
from app.services.immich import MEDIA_EXTENSIONS as IMMICH_MEDIA_EXTENSIONS
from app.services.organizer import organize_group
from app.services.sorting import parse_filename
from app.services.timezone_utils import configured_timezone, local_datetime, local_month

logger = logging.getLogger(__name__)
UNSAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
COVER_FILENAME_TAG = "\u005b\u5c01\u9762\u005d"
# 网络看门狗：检查间隔、单次探测超时、重连前的等待
WATCHDOG_INTERVAL = 60
WATCHDOG_PROBE_TIMEOUT = 5
WATCHDOG_RETRY_DELAY = 10
RECONNECT_TIMEOUT = 30
CHANNEL_LIST_TIMEOUT = 30
TELEGRAM_MESSAGE_LINK_RE = re.compile(
    r"https?://t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)",
    re.IGNORECASE,
)
BOT_RANGE_COMMAND_RE = re.compile(
    r"^\s*/(?:range|download_range)(?:@[A-Za-z0-9_]+)?\b",
    re.IGNORECASE,
)
BOT_ALBUM_COMMAND_RE = re.compile(
    r"^\s*/album(?:@[A-Za-z0-9_]+)?\b",
    re.IGNORECASE,
)
BOT_DOWNLOAD_COMMAND_RE = re.compile(
    r"^\s*/download(?:@[A-Za-z0-9_]+)?\b",
    re.IGNORECASE,
)
BOT_RANGE_LIMIT = 5000
# mimetypes maps video/quicktime to .mov but image/jpeg to .jpe on some
# platforms, and returns nothing for Telegram's common sticker/animation
# types. Pin the extensions Immich actually imports.
MIME_EXTENSION_OVERRIDES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpg",
    "video/3gpp": ".3gp",
    "video/x-ms-wmv": ".wmv",
    "video/x-flv": ".flv",
}


class TelegramSetupError(RuntimeError):
    pass


def _message_link_entity(match: re.Match[str]) -> int | str:
    private_id, username, _message_id = match.groups()
    return int(f"-100{private_id}") if private_id else str(username)


def parse_bot_range_request(text: str) -> dict[str, Any] | None:
    """Parse an explicit inclusive range request without changing plain links."""
    value = text.strip()
    matches = list(TELEGRAM_MESSAGE_LINK_RE.finditer(value))
    range_command = bool(BOT_RANGE_COMMAND_RE.match(value))
    download_command = bool(BOT_DOWNLOAD_COMMAND_RE.match(value))
    if not range_command and not download_command:
        return None

    entity: int | str
    start_id: int
    end_id: int
    if len(matches) >= 2:
        first_entity = _message_link_entity(matches[0])
        second_entity = _message_link_entity(matches[1])
        if str(first_entity) != str(second_entity):
            raise ValueError("区间起点和终点必须来自同一个 Telegram 频道")
        entity = first_entity
        start_id = int(matches[0].group(3))
        end_id = int(matches[1].group(3))
    elif download_command and len(matches) == 1:
        entity = _message_link_entity(matches[0])
        remainder = (
            value[: matches[0].start()] + " " + value[matches[0].end() :]
        )
        ids = [int(item) for item in re.findall(r"(?<!\d)(\d+)(?!\d)", remainder)]
        if len(ids) < 2:
            return None
        start_id, end_id = ids[-2:]
    elif range_command:
        raise ValueError("请同时提供起点消息链接和终点消息链接")
    else:
        return None

    lower, upper = sorted((start_id, end_id))
    total = upper - lower + 1
    if total > BOT_RANGE_LIMIT:
        raise ValueError(
            f"单次区间最多允许 {BOT_RANGE_LIMIT} 条消息，当前选择了 {total} 条"
        )
    return {
        "entity": entity,
        "start_message_id": lower,
        "end_message_id": upper,
        "message_count": total,
    }


def is_bot_album_request(text: str) -> bool:
    return bool(BOT_ALBUM_COMMAND_RE.match(text.strip()))


def parse_proxy_url(proxy_url: str) -> tuple | None:
    value = proxy_url.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    proxy_types = {
        "http": socks.HTTP,
        "https": socks.HTTP,
        "socks5": socks.SOCKS5,
        "socks4": socks.SOCKS4,
    }
    proxy_type = proxy_types.get(parsed.scheme.lower())
    if proxy_type is None or not parsed.hostname or not parsed.port:
        raise TelegramSetupError(
            "代理格式不正确，请使用 http://IP:端口 或 socks5://IP:端口"
        )
    return (
        proxy_type,
        parsed.hostname,
        parsed.port,
        True,
        parsed.username,
        parsed.password,
    )


class TelegramService:
    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]):
        self.settings = settings
        self.session_factory = session_factory
        self.client: TelegramClient | None = None
        self.status = "disabled"
        self._auth_lock = asyncio.Lock()
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self._client_identity: tuple[int, str, str] | None = None
        self._handler_registered = False
        self.bot_client: TelegramClient | None = None
        self._forward_tasks: dict[int, asyncio.Task] = {}
        self._forward_active: dict[int, dict[str, dict[str, Any]]] = {}
        self._organize_tasks: dict[int, asyncio.Task] = {}
        self._subject_idle_tasks: dict[int, asyncio.Task] = {}
        self._live_dispatcher: asyncio.Task | None = None
        self._live_catchup_task: asyncio.Task | None = None
        self._live_workers: dict[int, asyncio.Task] = {}
        self._live_active: dict[int, dict[str, Any]] = {}
        self._live_wakeup = asyncio.Event()
        self._live_paused = False
        self._live_cooldown_until = 0.0
        self._live_session_started = time.monotonic()
        self._live_session_bytes = 0
        self._media_download_locks: weakref.WeakValueDictionary[
            tuple[int, int], asyncio.Lock
        ] = weakref.WeakValueDictionary()
        self._watchdog_task: asyncio.Task | None = None
        # True only after _start_bot finished wiring handlers. A client that
        # exists but never finished start() answers probes yet cannot forward.
        self._bot_ready = False
        self.connection_error = ""
        self.bot_error = ""
        self.immich = None
        self.channel_reconcile_callback = None

    @property
    def session_file(self) -> Path:
        return (
            self.settings.storage.session_path
            / f"{self.settings.telegram.session_name}.session"
        )

    async def start(self) -> None:
        config = self.settings.telegram
        if not config.enabled:
            self.status = "disabled"
            return
        if not self.settings.telegram_api_id or not self.settings.telegram_api_hash:
            self.status = "missing_credentials"
            logger.warning("Telegram 已启用，但 API_ID/API_HASH 未配置")
            return
        try:
            client = await self._ensure_client()
            if not await client.is_user_authorized():
                self.status = "login_required"
                return
            await self._activate_listener()
            try:
                await self._start_bot()
            except Exception:
                logger.exception("机器人启动失败，频道监听仍保持运行")
        except Exception:
            self.status = "connection_error"
            logger.exception("Telegram 启动失败")

    async def stop(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watchdog_task
        self._watchdog_task = None
        await self._stop_live_queue()
        for task in self._subject_idle_tasks.values():
            task.cancel()
        if self._subject_idle_tasks:
            await asyncio.gather(
                *self._subject_idle_tasks.values(), return_exceptions=True
            )
        self._subject_idle_tasks.clear()
        for task in self._organize_tasks.values():
            task.cancel()
        self._organize_tasks.clear()
        for task in list(self._forward_tasks.values()):
            task.cancel()
        if self._forward_tasks:
            await asyncio.gather(
                *list(self._forward_tasks.values()), return_exceptions=True
            )
        self._forward_tasks.clear()
        if self.bot_client:
            await self.bot_client.disconnect()
            self.bot_client = None
        self._bot_ready = False
        if self.client:
            await self.client.disconnect()
        self.status = "stopped"

    async def setup_state(self) -> dict[str, Any]:
        authorized = False
        if self.client and self.client.is_connected():
            try:
                authorized = await asyncio.wait_for(
                    self.client.is_user_authorized(), timeout=5
                )
            except asyncio.TimeoutError:
                authorized = False
        return {
            "status": self.status,
            "credentials_saved": bool(
                self.settings.telegram_api_id and self.settings.telegram_api_hash
            ),
            "session_exists": self.session_file.exists(),
            "authorized": authorized or self.status == "running",
            "enabled": self.settings.telegram.enabled,
            "proxy_url": self.settings.telegram.proxy_url,
            "connection_error": self.connection_error,
            "channel": (
                self.settings.telegram.channels[0].model_dump()
                if self.settings.telegram.channels
                else None
            ),
            "channels": [
                channel.model_dump() for channel in self.settings.telegram.channels
            ],
            "downloads": self.live_download_status(),
            "bot": {
                "enabled": self.settings.telegram.bot_enabled,
                "configured": bool(self.settings.telegram_bot_token),
                "running": bool(
                    self.bot_client and self.bot_client.is_connected()
                ),
                "error": self.bot_error,
                "download_path": str(self.settings.storage.forwarded_path),
                "library_path": str(
                    self.settings.storage.forwarded_library_path
                ),
                "max_concurrent_downloads": (
                    self.settings.telegram.bot_max_concurrent_downloads
                ),
                "tasks": self._recent_forward_tasks(),
            },
        }

    def _recent_forward_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            rows = list(
                db.scalars(
                    select(Task)
                    .where(Task.kind == "bot_forward")
                    .order_by(Task.created_at.desc())
                    .limit(limit)
                )
            )
        result = []
        for row in rows:
            payload = json.loads(row.payload_json or "{}")
            result.append(
                {
                    "id": row.id,
                    "task_id": payload.get("task_id", f"F{row.id:06d}"),
                    "status": row.status,
                    "total": int(payload.get("total", 0)),
                    "success": int(payload.get("success", 0)),
                    "failed": int(payload.get("failed", 0)),
                    "skipped": int(payload.get("skipped", 0)),
                    "description": payload.get("description", ""),
                    "output_path": payload.get("output_path", ""),
                    "library_path": payload.get("library_path", ""),
                    "current_file": payload.get("current_file", ""),
                    "speed_bps": float(payload.get("speed_bps", 0) or 0),
                    "active_count": int(payload.get("active_count", 0) or 0),
                    "max_concurrent_downloads": int(
                        payload.get("max_concurrent_downloads", 5) or 5
                    ),
                    "error": row.error,
                }
            )
        return result

    def forward_download_status(self) -> dict[str, Any]:
        tasks = self._recent_forward_tasks(limit=30)
        return {
            "paused": not any(
                item["status"] == "running" for item in tasks
            ),
            "active_tasks": sum(
                item["status"] == "running" for item in tasks
            ),
            "tasks": tasks,
        }

    async def pause_forward_task(self, task_id: int) -> dict[str, Any]:
        with self.session_factory() as db:
            row = db.get(Task, task_id)
            if not row or row.kind != "bot_forward":
                raise TelegramSetupError("机器人下载任务不存在")
            if row.status in {"completed", "failed"}:
                raise TelegramSetupError("已结束的任务不能暂停")
            row.status = "paused"
            db.commit()
        running = self._forward_tasks.get(task_id)
        if running and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        return {"id": task_id, "status": "paused", "message": "任务已暂停"}

    async def resume_forward_task(self, task_id: int) -> dict[str, Any]:
        with self.session_factory() as db:
            row = db.get(Task, task_id)
            if not row or row.kind != "bot_forward":
                raise TelegramSetupError("机器人下载任务不存在")
            if row.status == "completed":
                return {
                    "id": task_id,
                    "status": "completed",
                    "message": "任务已经完成",
                }
        # Reconnect first: a dead bot cancels stalled tasks and rewinds their
        # rows, which would otherwise overwrite the queued state set below.
        await self._ensure_bot_online()
        with self.session_factory() as db:
            row = db.get(Task, task_id)
            if not row or row.kind != "bot_forward":
                raise TelegramSetupError("机器人下载任务不存在")
            payload = json.loads(row.payload_json or "{}")
            for source in payload.get("sources", []):
                if source.get("status") == "failed":
                    source["status"] = "queued"
                    source["error"] = ""
            row.payload_json = json.dumps(payload, ensure_ascii=False)
            row.status = "queued"
            row.error = None
            row.finished_at = None
            db.commit()
        self._launch_forward_task(task_id)
        return {"id": task_id, "status": "queued", "message": "任务已继续"}

    async def configure_bot(
        self,
        *,
        enabled: bool,
        token: str,
        max_concurrent_downloads: int = 5,
    ) -> dict[str, Any]:
        resolved_token = token.strip() or self.settings.telegram_bot_token or ""
        if enabled and not resolved_token:
            raise TelegramSetupError("启用机器人前请填写 Bot Token")
        client = await self._ensure_client()
        if not await self._authorized_within_timeout(client):
            raise TelegramSetupError("请先登录 Telegram 用户账号")
        me = await asyncio.wait_for(client.get_me(), timeout=RECONNECT_TIMEOUT)
        allowed_ids = [int(me.id)]
        save_telegram_bot_config(
            enabled=enabled,
            token=token,
            allowed_user_ids=allowed_ids,
            max_concurrent_downloads=max_concurrent_downloads,
        )
        self.settings.telegram.bot_enabled = enabled
        self.settings.telegram.bot_allowed_user_ids = allowed_ids
        self.settings.telegram.bot_max_concurrent_downloads = (
            max_concurrent_downloads
        )
        for task in list(self._forward_tasks.values()):
            task.cancel()
        if self._forward_tasks:
            await asyncio.gather(
                *list(self._forward_tasks.values()), return_exceptions=True
            )
        self._forward_tasks.clear()
        if self.bot_client:
            await self.bot_client.disconnect()
            self.bot_client = None
        if enabled:
            try:
                await self._start_bot()
            except Exception as exc:
                raise TelegramSetupError(f"机器人连接失败：{exc}") from exc
        return {
            "enabled": enabled,
            "configured": bool(resolved_token),
            "running": bool(self.bot_client and self.bot_client.is_connected()),
            "message": (
                "机器人转发下载已启用，只有当前 Telegram 账号可以使用"
                if enabled
                else "机器人转发下载已停用"
            ),
        }

    async def _start_bot(self) -> None:
        if not self.settings.telegram.bot_enabled:
            return
        token = self.settings.telegram_bot_token
        if not token or not self.settings.telegram_api_id or not self.settings.telegram_api_hash:
            logger.warning("机器人下载已启用，但 Bot Token 或 Telegram API 信息缺失")
            return
        if self.bot_client:
            await self.bot_client.disconnect()
        self._bot_ready = False
        self.bot_client = TelegramClient(
            str(self.settings.storage.session_path / "xuying-bot"),
            self.settings.telegram_api_id,
            self.settings.telegram_api_hash,
            proxy=parse_proxy_url(self.settings.telegram.proxy_url),
        )
        await self.bot_client.start(bot_token=token)
        self.bot_client.add_event_handler(self._on_bot_message, events.NewMessage(incoming=True))
        self._bot_ready = True
        try:
            await self.bot_client(
                SetBotCommandsRequest(
                    scope=BotCommandScopeDefault(),
                    lang_code="",
                    commands=[
                        BotCommand("start", "开始使用并查看中文说明"),
                        BotCommand("help", "查看下载方式和命令示例"),
                        BotCommand("album", "下载一整个 Telegram 相册"),
                        BotCommand("range", "下载起点到终点的连续区间"),
                        BotCommand("status", "查看最近机器人下载任务"),
                    ],
                )
            )
        except Exception:
            logger.warning("Telegram 机器人命令菜单注册失败", exc_info=True)
        logger.info("Telegram 机器人转发下载已启动")
        await self._recover_forward_tasks()

    async def _on_bot_message(self, event: Any) -> None:
        if int(event.sender_id or 0) not in set(
            self.settings.telegram.bot_allowed_user_ids
        ):
            await event.reply("这个机器人只允许序影绑定账号使用。")
            return
        message = event.message
        command = (message.raw_text or "").strip().split(maxsplit=1)[0]
        command = command.split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            await event.reply(self._bot_help_text())
            return
        if command == "/status":
            await event.reply(self._bot_status_text())
            return
        try:
            sources: list[tuple[Any, dict[str, Any]]] = []
            range_descriptions: list[str] = []
            range_request = parse_bot_range_request(message.raw_text or "")
            if message.media:
                sources.append(
                    (
                        message,
                        {
                            "client": "bot",
                            "entity": int(event.chat_id),
                            "message_id": int(message.id),
                            "label": f"B{event.sender_id}_{message.id}",
                        },
                    )
                )
            if range_request and self.client:
                range_messages = await self.client.get_messages(
                    range_request["entity"],
                    ids=list(
                        range(
                            int(range_request["start_message_id"]),
                            int(range_request["end_message_id"]) + 1,
                        )
                    ),
                )
                for item in sorted(
                    [candidate for candidate in range_messages if candidate],
                    key=lambda candidate: int(candidate.id),
                ):
                    text = (getattr(item, "message", None) or "").strip()
                    if text and text not in range_descriptions:
                        range_descriptions.append(text)
                    if not item.media:
                        continue
                    sources.append(
                        (
                            item,
                            {
                                "client": "user",
                                "entity": range_request["entity"],
                                "message_id": int(item.id),
                                "label": (
                                    f"R{str(range_request['entity']).replace('-', 'n')}"
                                    f"_{item.id}"
                                ),
                            },
                        )
                    )
            links = (
                []
                if range_request
                else TELEGRAM_MESSAGE_LINK_RE.findall(message.raw_text or "")
            )
            expand_album = is_bot_album_request(message.raw_text or "")
            for private_id, username, message_id in links:
                if not self.client:
                    continue
                entity: int | str = (
                    int(f"-100{private_id}") if private_id else username
                )
                source = await self.client.get_messages(entity, ids=int(message_id))
                if not source:
                    continue
                source_messages = [source]
                grouped_id = getattr(source, "grouped_id", None)
                if expand_album and grouped_id:
                    nearby = await self.client.get_messages(
                        entity,
                        ids=list(range(max(1, int(source.id) - 20), int(source.id) + 21)),
                    )
                    source_messages = sorted(
                        [
                            item
                            for item in nearby
                            if item
                            and item.media
                            and getattr(item, "grouped_id", None) == grouped_id
                        ],
                        key=lambda item: int(item.id),
                    )
                for item in source_messages:
                    if not item.media:
                        continue
                    sources.append(
                        (
                            item,
                            {
                                "client": "user",
                                "entity": entity,
                                "message_id": int(item.id),
                                "label": (
                                    f"L{str(entity).replace('-', 'n')}_{item.id}"
                                ),
                            },
                        )
                    )
            # A direct forwarded media and a link can point to the same object.
            deduplicated: dict[
                tuple[str, int], tuple[Any, dict[str, Any]]
            ] = {}
            for source, descriptor in sources:
                key = (
                    str(
                        getattr(
                            source, "chat_id", descriptor.get("entity", "")
                        )
                    ),
                    int(getattr(source, "id", 0)),
                )
                descriptor.update(
                    {
                        "key": f"{descriptor['client']}:{descriptor['entity']}:{source.id}",
                        "caption": (
                            getattr(source, "message", None) or ""
                        ).strip(),
                        "message_date": (
                            getattr(source, "date", None).isoformat()
                            if getattr(source, "date", None)
                            else ""
                        ),
                        "original_name": self._original_filename(source),
                        "size_bytes": int(
                            getattr(
                                getattr(source, "file", None), "size", 0
                            )
                            or 0
                        ),
                        "status": "queued",
                        "filename": "",
                        "error": "",
                    }
                )
                deduplicated[key] = (source, descriptor)
            sources = list(deduplicated.values())
            for order_index, (_source, descriptor) in enumerate(sources):
                descriptor["order_index"] = order_index
            if not sources:
                await event.reply(
                    "没有识别到可下载媒体。\n"
                    "• 单条：直接转发媒体或发送消息链接\n"
                    "• 完整相册：/album 消息链接\n"
                    "• 连续区间：/range 起点链接 终点链接"
                )
                return

            descriptions = list(range_descriptions)
            for source, _descriptor in sources:
                caption = (getattr(source, "message", None) or "").strip()
                if caption and caption not in descriptions:
                    descriptions.append(caption)
            description = "\n\n".join(descriptions)
            now = datetime.now(timezone.utc)
            local_now = local_datetime(now, self.settings.app.timezone)
            with self.session_factory() as db:
                row = Task(
                    kind="bot_forward",
                    status="running",
                    progress=0,
                    payload_json="{}",
                    started_at=now,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                task_row_id = int(row.id)
                task_display = f"F{local_now.strftime('%m%d')}-{row.id:04d}"
                folder_label = self._safe_filename(
                    description.splitlines()[0][:28]
                    if description
                    else "无简介"
                )
                task_dir = (
                    self.settings.storage.forwarded_path
                    / local_now.strftime("%Y_%m")
                    / f"{task_display}_{folder_label}"
                )
                task_dir.mkdir(parents=True, exist_ok=True)
                payload = {
                    "task_id": task_display,
                    "total": len(sources),
                    "success": 0,
                    "failed": 0,
                    "skipped": 0,
                    "description": description,
                    "output_path": str(task_dir),
                    "files": [],
                    "current_file": "",
                    "speed_bps": 0,
                    "throttled": False,
                    "active_count": 0,
                    "max_concurrent_downloads": (
                        self.settings.telegram.bot_max_concurrent_downloads
                    ),
                    "sources": [descriptor for _source, descriptor in sources],
                    "bot_chat_id": int(event.chat_id),
                    "status_message_id": 0,
                    "request_text": message.raw_text or "",
                    "selection_mode": (
                        "inclusive_range" if range_request else "single_or_album"
                    ),
                    "range": range_request or {},
                }
                row.payload_json = json.dumps(payload, ensure_ascii=False)
                db.commit()

            self._write_forward_metadata(
                task_dir=task_dir,
                task_id=task_display,
                description=description,
                request_text=message.raw_text or "",
                files=[],
            )
            status_message = await event.reply(
                self._forward_status_text(payload, phase="已接收任务")
            )
            with self.session_factory() as db:
                row = db.get(Task, task_row_id)
                if row:
                    payload["status_message_id"] = int(status_message.id)
                    row.payload_json = json.dumps(payload, ensure_ascii=False)
                    db.commit()
            source_objects = {
                descriptor["key"]: source for source, descriptor in sources
            }
            self._launch_forward_task(
                task_row_id,
                source_objects=source_objects,
                status_message=status_message,
            )
        except Exception as exc:
            logger.exception("机器人转发下载失败")
            await event.reply(f"下载任务创建失败：{exc}")

    @staticmethod
    def _bot_help_text() -> str:
        return (
            "🎞 序影机器人 · Telegram → Immich\n\n"
            "我会把机器人下载与频道人物库完全分开，"
            "每个机器人任务自动建立独立 Immich 相册。\n\n"
            "① 下载单独一条\n"
            "直接转发一张照片/视频，或直接发送一个消息链接。\n"
            "普通转发和普通链接都只下载这一条，不会自动扩大范围。\n\n"
            "② 下载完整 Telegram 相册\n"
            "/album 消息链接\n"
            "示例：/album https://t.me/c/123456/100\n\n"
            "③ 下载连续消息区间\n"
            "/range 起点消息链接 终点消息链接\n"
            "起点、终点以及中间所有照片和视频都会包含，"
            "并整理到同一个任务相册。\n\n"
            "④ Hermes 兼容区间命令\n"
            "/download 频道消息链接 起始ID 结束ID\n\n"
            "⑤ 查看任务\n"
            "/status\n\n"
            "任务支持并发下载、网页实时进度、暂停/继续，"
            "容器意外重启后会接着未完成部分下载。"
        )

    def _bot_status_text(self) -> str:
        tasks = self._recent_forward_tasks(limit=5)
        if not tasks:
            return "📭 目前还没有机器人下载任务。发送 /help 查看使用方法。"
        status_names = {
            "queued": "排队中",
            "running": "下载中",
            "paused": "已暂停",
            "completed": "已完成",
            "failed": "失败",
        }
        lines = ["📊 序影机器人 · 最近任务"]
        for task in tasks:
            finished = int(task.get("success", 0)) + int(
                task.get("skipped", 0)
            )
            lines.append(
                f"\n{task.get('task_id', '—')} · "
                f"{status_names.get(task.get('status'), task.get('status', '—'))}\n"
                f"{finished}/{task.get('total', 0)}｜"
                f"失败 {task.get('failed', 0)}｜"
                f"速度 {self._format_speed(float(task.get('speed_bps', 0) or 0))}"
            )
        lines.append("\n详细进度和暂停/继续操作请打开序影网页。")
        return "".join(lines)

    @staticmethod
    def _format_speed(speed_bps: float) -> str:
        if speed_bps >= 1024 * 1024:
            return f"{speed_bps / 1024 / 1024:.1f} MB/s"
        return f"{speed_bps / 1024:.0f} KB/s"

    def _launch_forward_task(
        self,
        task_id: int,
        *,
        source_objects: dict[str, Any] | None = None,
        status_message: Any | None = None,
    ) -> None:
        current = self._forward_tasks.get(task_id)
        if current and not current.done():
            return
        task = asyncio.create_task(
            self._run_forward_task(
                task_id,
                source_objects=source_objects or {},
                status_message=status_message,
            )
        )
        self._forward_tasks[task_id] = task
        task.add_done_callback(
            lambda _done, resolved=task_id: self._forward_tasks.pop(
                resolved, None
            )
        )

    async def _recover_forward_tasks(self) -> None:
        with self.session_factory() as db:
            rows = list(
                db.scalars(
                    select(Task).where(
                        Task.kind == "bot_forward",
                        Task.status.in_(["queued", "running"]),
                    )
                )
            )
            for row in rows:
                row.status = "queued"
            db.commit()
        for row in rows:
            self._launch_forward_task(int(row.id))

    async def _forward_status_message(
        self, payload: dict[str, Any]
    ) -> Any | None:
        if not self.bot_client:
            return None
        chat_id = int(payload.get("bot_chat_id", 0) or 0)
        message_id = int(payload.get("status_message_id", 0) or 0)
        if not chat_id or not message_id:
            return None
        with suppress(Exception):
            return await self.bot_client.get_messages(chat_id, ids=message_id)
        return None

    async def _forward_source_message(
        self, descriptor: dict[str, Any]
    ) -> Any:
        client = (
            self.bot_client
            if descriptor.get("client") == "bot"
            else self.client
        )
        if not client:
            raise RuntimeError("Telegram 客户端尚未连接")
        message = await client.get_messages(
            descriptor["entity"], ids=int(descriptor["message_id"])
        )
        if not message or not message.media:
            raise RuntimeError(
                f"消息 {descriptor['message_id']} 已不存在或没有媒体"
            )
        return message

    async def _run_forward_task(
        self,
        task_id: int,
        *,
        source_objects: dict[str, Any],
        status_message: Any | None,
    ) -> None:
        with self.session_factory() as db:
            row = db.get(Task, task_id)
            if not row or row.status == "paused":
                return
            payload = json.loads(row.payload_json or "{}")
            if not payload.get("sources"):
                row.status = "failed"
                row.error = "旧版机器人任务没有断点元数据，请重新转发该消息"
                row.finished_at = datetime.now(timezone.utc)
                db.commit()
                return
            row.status = "running"
            row.started_at = row.started_at or datetime.now(timezone.utc)
            row.error = None
            db.commit()
        if status_message is None:
            status_message = await self._forward_status_message(payload)

        active = self._forward_active.setdefault(task_id, {})
        semaphore = asyncio.Semaphore(
            max(
                1,
                min(
                    8,
                    int(
                        payload.get("max_concurrent_downloads")
                        or self.settings.telegram.bot_max_concurrent_downloads
                    ),
                ),
            )
        )
        payload_lock = asyncio.Lock()
        task_dir = Path(payload["output_path"])
        task_dir.mkdir(parents=True, exist_ok=True)

        async def worker(descriptor: dict[str, Any]) -> None:
            async with semaphore:
                key = descriptor["key"]
                state = {
                    "received": 0,
                    "total": int(descriptor.get("size_bytes", 0) or 0),
                    "speed_bps": 0.0,
                    "current_file": descriptor.get("original_name") or "",
                }
                active[key] = state
                try:
                    source = source_objects.get(key)
                    if source is None:
                        source = await self._forward_source_message(descriptor)
                    destination, skipped = await self._download_forwarded_message(
                        source,
                        source_label=descriptor["label"],
                        destination_dir=task_dir,
                        progress_state=state,
                    )
                    self._write_forward_description_sidecar(
                        destination,
                        descriptor.get("caption")
                        or payload.get("description", ""),
                        taken_at=descriptor.get("message_date", ""),
                        order_index=int(descriptor.get("order_index", 0)),
                    )
                    descriptor.update(
                        {
                            "status": "skipped" if skipped else "completed",
                            "filename": destination.name,
                            "size_bytes": destination.stat().st_size,
                            "error": "",
                        }
                    )
                except asyncio.CancelledError:
                    descriptor["status"] = "queued"
                    raise
                except Exception as exc:
                    descriptor["status"] = "failed"
                    descriptor["error"] = str(exc)
                finally:
                    active.pop(key, None)
                    async with payload_lock:
                        self._refresh_forward_payload(payload)
                        self._update_forward_task(
                            task_id,
                            payload,
                            progress=payload["success"] + payload["skipped"],
                        )

        monitor = asyncio.create_task(
            self._monitor_forward_task(task_id, payload, status_message)
        )
        try:
            pending = [
                item
                for item in payload.get("sources", [])
                if item.get("status") not in {"completed", "skipped"}
            ]
            await asyncio.gather(*(worker(item) for item in pending))
            self._refresh_forward_payload(payload)
            payload["current_file"] = ""
            payload["active_count"] = 0
            payload["files"] = [
                {
                    "message_id": item.get("message_id"),
                    "filename": item.get("filename")
                    or item.get("original_name"),
                    "caption": item.get("caption", ""),
                    "size_bytes": item.get("size_bytes", 0),
                    **({"error": item["error"]} if item.get("error") else {}),
                }
                for item in payload.get("sources", [])
            ]
            self._write_forward_metadata(
                task_dir=task_dir,
                task_id=payload["task_id"],
                description=payload.get("description", ""),
                request_text=payload.get("request_text", ""),
                files=payload["files"],
            )
            library_dir = self._organize_forward_task(task_dir)
            payload["library_path"] = str(library_dir)
            final_status = "completed" if payload["failed"] == 0 else "failed"
            with self.session_factory() as db:
                row = db.get(Task, task_id)
                if row:
                    row.status = final_status
                    row.progress = payload["success"] + payload["skipped"]
                    row.payload_json = json.dumps(payload, ensure_ascii=False)
                    row.error = (
                        None
                        if final_status == "completed"
                        else f"{payload['failed']} 个媒体下载失败"
                    )
                    row.finished_at = datetime.now(timezone.utc)
                    db.commit()
            if status_message:
                await self._safe_bot_edit(
                    status_message,
                    self._forward_status_text(
                        payload,
                        phase=(
                            "任务完成并已进入独立媒体库"
                            if final_status == "completed"
                            else "任务完成，但有文件失败"
                        ),
                    ),
                )
            if self.immich:
                self.immich.schedule_forwarded_refresh()
        except asyncio.CancelledError:
            with self.session_factory() as db:
                row = db.get(Task, task_id)
                if row and row.status != "paused":
                    row.status = "queued"
                    row.payload_json = json.dumps(payload, ensure_ascii=False)
                    db.commit()
            raise
        finally:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
            self._forward_active.pop(task_id, None)

    @staticmethod
    def _refresh_forward_payload(payload: dict[str, Any]) -> None:
        sources = payload.get("sources", [])
        payload["success"] = sum(
            item.get("status") == "completed" for item in sources
        )
        payload["skipped"] = sum(
            item.get("status") == "skipped" for item in sources
        )
        payload["failed"] = sum(
            item.get("status") == "failed" for item in sources
        )

    async def _monitor_forward_task(
        self,
        task_id: int,
        payload: dict[str, Any],
        status_message: Any | None,
    ) -> None:
        low_since = 0.0
        slow_notified = False
        while True:
            await asyncio.sleep(3)
            states = list(self._forward_active.get(task_id, {}).values())
            speed = sum(float(item.get("speed_bps", 0) or 0) for item in states)
            payload["speed_bps"] = speed
            payload["active_count"] = len(states)
            names = [
                str(item.get("current_file", ""))
                for item in states
                if item.get("current_file")
            ]
            payload["current_file"] = "、".join(names[:3])
            now = time.monotonic()
            if states and 0 < speed < 200 * 1024:
                low_since = low_since or now
                payload["throttled"] = now - low_since >= 120
            else:
                low_since = 0.0
                payload["throttled"] = False
            if payload["throttled"] and not slow_notified and status_message:
                await self._safe_bot_notify(
                    status_message,
                    (
                        "🐌 TG 下载疑似被限速\n"
                        f"任务：{payload.get('task_id', '')}\n"
                        "总速度持续低于 200 KB/s 达 120 秒，"
                        "任务会保留并自动继续。"
                    ),
                )
                slow_notified = True
            elif not payload["throttled"] and slow_notified and status_message:
                await self._safe_bot_notify(
                    status_message,
                    (
                        "✅ TG 限速已解除\n"
                        f"任务：{payload.get('task_id', '')}\n"
                        "下载速度已经恢复。"
                    ),
                )
                slow_notified = False
            self._update_forward_task(
                task_id,
                payload,
                progress=payload.get("success", 0)
                + payload.get("skipped", 0),
            )
            if status_message:
                await self._safe_bot_edit(
                    status_message,
                    self._forward_status_text(payload, phase="正在并发下载"),
                )

    def _organize_forward_task(self, task_dir: Path) -> Path:
        relative = task_dir.relative_to(self.settings.storage.forwarded_path)
        target_dir = self.settings.storage.forwarded_library_path / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for source in task_dir.iterdir():
            if not source.is_file() or source.name.startswith("."):
                continue
            target = target_dir / source.name
            if target.exists():
                continue
            os.link(source, target)
        return target_dir

    async def _download_forwarded_message(
        self,
        message: Any,
        *,
        source_label: str,
        destination_dir: Path,
        progress_state: dict[str, Any],
    ) -> tuple[Path, bool]:
        original_name = self._original_filename(message)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / (
            f"{self._safe_filename(source_label)} - "
            f"{self._safe_filename(original_name)}"
        )
        if destination.is_file():
            return destination, True
        partial = destination.with_name(f".{destination.name}.part")
        started = time.monotonic()
        last_received = [0]
        last_time = [started]

        def report(received: int, total: int) -> None:
            now = time.monotonic()
            elapsed = max(now - last_time[0], 0.001)
            delta = max(0, int(received) - last_received[0])
            progress_state["received"] = int(received)
            progress_state["total"] = int(total or 0)
            progress_state["speed_bps"] = delta / elapsed
            last_received[0] = int(received)
            last_time[0] = now

        for attempt in range(1, 6):
            try:
                if partial.exists():
                    partial.unlink()
                await message.download_media(
                    file=str(partial), progress_callback=report
                )
                break
            except FloodWaitError as exc:
                progress_state["flood_wait"] = int(exc.seconds)
                await asyncio.sleep(max(1, int(exc.seconds)) + 1)
                progress_state.pop("flood_wait", None)
            except (
                FileReferenceExpiredError,
                TimedOutError,
                RpcCallFailError,
                ServerError,
                OSError,
            ):
                if attempt >= 5:
                    raise
                await asyncio.sleep((5, 15, 30, 60)[min(attempt - 1, 3)])
        if not partial.is_file():
            raise RuntimeError(f"媒体 {message.id} 下载后未找到临时文件")
        os.replace(partial, destination)
        if not destination.is_file():
            raise RuntimeError(f"媒体 {message.id} 下载后未找到文件")
        return destination, False

    async def _monitor_forward_download(
        self,
        status_message: Any,
        payload: dict[str, Any],
        state: dict[str, Any],
    ) -> None:
        """Update one bot bubble and warn only after sustained low speed."""
        while not state.get("done"):
            await asyncio.sleep(8)
            now = time.monotonic()
            speed = float(state.get("speed_bps", 0) or 0)
            payload["speed_bps"] = speed
            flood_wait = int(state.get("flood_wait", 0) or 0)
            if flood_wait:
                phase = f"Telegram 限速，{flood_wait} 秒后自动继续"
                if not state.get("flood_notified"):
                    await self._safe_bot_notify(
                        status_message,
                        (
                            "⏳ Telegram 要求暂时等待\n"
                            f"任务：{payload.get('task_id', '')}\n"
                            f"{flood_wait} 秒后序影会自动继续，不需要手动操作。"
                        ),
                    )
                    state["flood_notified"] = True
            else:
                if state.pop("flood_notified", False):
                    await self._safe_bot_notify(
                        status_message,
                        (
                            "✅ Telegram 等待结束\n"
                            f"任务：{payload.get('task_id', '')}\n"
                            "下载已自动恢复。"
                        ),
                    )
                if 0 < speed < 200 * 1024:
                    state["low_since"] = state.get("low_since") or now
                    if now - float(state["low_since"]) >= 120:
                        state["throttled"] = True
                else:
                    state["low_since"] = 0.0
                    state["throttled"] = False
                phase = (
                    "疑似持续限速，序影会自动等待并重试"
                    if state.get("throttled")
                    else "正在下载"
                )
                if state.get("throttled") and not state.get(
                    "slow_notified"
                ):
                    await self._safe_bot_notify(
                        status_message,
                        (
                            "🐌 TG 下载疑似被限速\n"
                            f"任务：{payload.get('task_id', '')}\n"
                            f"文件：{payload.get('current_file') or '—'}\n"
                            "速度持续低于 200 KB/s 达 120 秒，"
                            "序影会保持任务并自动继续。"
                        ),
                    )
                    state["slow_notified"] = True
                elif not state.get("throttled") and state.pop(
                    "slow_notified", False
                ):
                    await self._safe_bot_notify(
                        status_message,
                        (
                            "✅ TG 限速已解除\n"
                            f"任务：{payload.get('task_id', '')}\n"
                            "下载速度已经恢复。"
                        ),
                    )
            payload["throttled"] = bool(
                state.get("throttled") or flood_wait
            )
            await self._safe_bot_edit(
                status_message,
                self._forward_status_text(
                    payload,
                    phase=phase,
                    received=int(state.get("received", 0)),
                    total_bytes=int(state.get("total", 0)),
                ),
            )

    @staticmethod
    def _forward_status_text(
        payload: dict[str, Any],
        *,
        phase: str,
        received: int = 0,
        total_bytes: int = 0,
    ) -> str:
        speed = float(payload.get("speed_bps", 0) or 0)
        speed_text = (
            f"{speed / 1024 / 1024:.1f} MB/s"
            if speed >= 1024 * 1024
            else f"{speed / 1024:.0f} KB/s"
        )
        progress = ""
        if total_bytes:
            progress = (
                f"\n当前进度：{received / 1024 / 1024:.1f} / "
                f"{total_bytes / 1024 / 1024:.1f} MB"
            )
        description = (payload.get("description") or "").strip()
        description_line = (
            f"\n简介：{description[:120]}"
            if description
            else "\n简介：无"
        )
        return (
            f"📥 序影下载 · {phase}\n"
            f"任务：{payload.get('task_id', '')}\n"
            f"总数：{payload.get('total', 0)}｜"
            f"成功：{payload.get('success', 0)}｜"
            f"跳过：{payload.get('skipped', 0)}｜"
            f"失败：{payload.get('failed', 0)}\n"
            f"并发：{payload.get('active_count', 0)} / "
            f"{payload.get('max_concurrent_downloads', 1)}\n"
            f"文件：{payload.get('current_file') or '—'}\n"
            f"速度：{speed_text}"
            f"{progress}{description_line}"
        )

    @staticmethod
    async def _safe_bot_edit(message: Any, text: str) -> None:
        try:
            await message.edit(text)
        except FloodWaitError as exc:
            await asyncio.sleep(max(1, int(exc.seconds)) + 1)
            with suppress(Exception):
                await message.edit(text)
        except Exception:
            logger.debug("机器人状态消息更新失败", exc_info=True)

    @staticmethod
    async def _safe_bot_notify(message: Any, text: str) -> None:
        try:
            await message.respond(text)
        except FloodWaitError as exc:
            await asyncio.sleep(max(1, int(exc.seconds)) + 1)
            with suppress(Exception):
                await message.respond(text)
        except Exception:
            logger.debug("机器人通知发送失败", exc_info=True)

    def _update_forward_task(
        self, task_id: int, payload: dict[str, Any], *, progress: int
    ) -> None:
        with self.session_factory() as db:
            row = db.get(Task, task_id)
            if row:
                row.progress = progress
                row.payload_json = json.dumps(payload, ensure_ascii=False)
                db.commit()

    @staticmethod
    def _write_forward_metadata(
        *,
        task_dir: Path,
        task_id: str,
        description: str,
        request_text: str,
        files: list[dict[str, Any]],
    ) -> None:
        intro = description.strip() or "此任务没有附带简介。"
        (task_dir / "简介.txt").write_text(intro + "\n", encoding="utf-8")
        (task_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "description": description,
                    "request_text": request_text,
                    "files": files,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _write_forward_description_sidecar(
        media_path: Path,
        description: str,
        *,
        taken_at: str = "",
        order_index: int = 0,
    ) -> Path:
        sidecar = media_path.with_name(media_path.name + ".xmp")
        value = escape(description.strip())
        ordered_time = ""
        if taken_at:
            with suppress(ValueError):
                resolved = datetime.fromisoformat(taken_at.replace("Z", "+00:00"))
                resolved += timedelta(microseconds=max(0, order_index))
                ordered_time = resolved.isoformat(timespec="microseconds")
        time_attributes = (
            f'\n      xmp:CreateDate="{escape(ordered_time)}"'
            f'\n      photoshop:DateCreated="{escape(ordered_time)}"'
            if ordered_time
            else ""
        )
        sidecar.write_text(
            f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
      xmlns:dc="http://purl.org/dc/elements/1.1/"
      xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"{time_attributes}>
      <dc:description>
        <rdf:Alt>
          <rdf:li xml:lang="x-default">{value}</rdf:li>
        </rdf:Alt>
      </dc:description>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
""",
            encoding="utf-8",
        )
        return sidecar

    async def request_login_code(
        self,
        *,
        api_id: int,
        api_hash: str,
        phone: str,
        proxy_url: str,
    ) -> dict[str, Any]:
        async with self._auth_lock:
            if (
                self.status == "running"
                and self.client
                and await self._authorized_within_timeout(self.client)
            ):
                return {
                    "status": "authorized",
                    "message": "序影已经登录，为保护现有 Session，不会重复发起登录",
                }
            save_runtime_secrets(api_id, api_hash, phone)
            self.settings.telegram.proxy_url = proxy_url.strip()
            client = await self._ensure_client(
                api_id=api_id,
                api_hash=api_hash.strip(),
                proxy_url=proxy_url,
                force_new=True,
            )
            if await client.is_user_authorized():
                self.status = "authorized"
                return {"status": "authorized", "message": "现有 Session 已登录"}
            try:
                sent = await client.send_code_request(phone)
            except FloodWaitError as exc:
                raise TelegramSetupError(
                    f"Telegram 请求过于频繁，请等待 {exc.seconds} 秒后再试"
                ) from exc
            except Exception as exc:
                self.status = "connection_error"
                raise TelegramSetupError(f"验证码发送失败：{exc}") from exc
            self._phone = phone
            self._phone_code_hash = sent.phone_code_hash
            self.status = "code_sent"
            return {
                "status": "code_sent",
                "message": "验证码已发送到你的 Telegram 客户端",
            }

    async def confirm_login_code(self, code: str) -> dict[str, str]:
        async with self._auth_lock:
            if not self.client or not self._phone:
                raise TelegramSetupError("登录流程已失效，请重新发送验证码")
            try:
                await self.client.sign_in(
                    phone=self._phone,
                    code=code.strip(),
                    phone_code_hash=self._phone_code_hash,
                )
            except SessionPasswordNeededError:
                self.status = "password_required"
                return {
                    "status": "password_required",
                    "message": "账号已开启两步验证，请输入 Telegram 两步验证密码",
                }
            except PhoneCodeInvalidError as exc:
                raise TelegramSetupError("验证码不正确，请检查后重试") from exc
            except PhoneCodeExpiredError as exc:
                raise TelegramSetupError("验证码已过期，请重新发送") from exc
            except Exception as exc:
                raise TelegramSetupError(f"登录失败：{exc}") from exc
            self.status = "authorized"
            return {"status": "authorized", "message": "Telegram 登录成功"}

    async def confirm_password(self, password: str) -> dict[str, str]:
        async with self._auth_lock:
            if not self.client:
                raise TelegramSetupError("登录流程已失效，请重新开始")
            try:
                await self.client.sign_in(password=password)
            except PasswordHashInvalidError as exc:
                raise TelegramSetupError("两步验证密码不正确") from exc
            except Exception as exc:
                raise TelegramSetupError(f"两步验证失败：{exc}") from exc
            self.status = "authorized"
            return {"status": "authorized", "message": "Telegram 登录成功"}

    async def activate(
        self,
        *,
        channel_name: str,
        chat_id: int,
        start_message_id: int,
        start_mode: str = "message_id",
        start_date: date | None = None,
        proxy_url: str,
        grouping_mode: str = "telegram_album",
        marker_text: str = "1",
        advertisement_policy: str = "quarantine",
        advertisement_keywords: list[str] | None = None,
        display_spacing_hours: int = 24,
        timeline_mode: str = "album",
        display_order: int = 0,
        max_concurrent_downloads: int = 5,
    ) -> dict[str, Any]:
        return await self.upsert_channel(
            channel_name=channel_name,
            chat_id=chat_id,
            start_message_id=start_message_id,
            start_mode=start_mode,
            start_date=start_date,
            enabled=True,
            grouping_mode=grouping_mode,
            marker_text=marker_text,
            advertisement_policy=advertisement_policy,
            advertisement_keywords=advertisement_keywords,
            display_spacing_hours=display_spacing_hours,
            timeline_mode=timeline_mode,
            display_order=display_order,
            max_concurrent_downloads=max_concurrent_downloads,
            proxy_url=proxy_url,
        )

    async def upsert_channel(
        self,
        *,
        channel_name: str,
        chat_id: int,
        start_message_id: int,
        start_mode: str = "message_id",
        start_date: date | None = None,
        enabled: bool,
        grouping_mode: str,
        marker_text: str,
        advertisement_policy: str,
        advertisement_keywords: list[str] | None,
        display_spacing_hours: int,
        timeline_mode: str,
        display_order: int,
        max_concurrent_downloads: int,
        proxy_url: str | None = None,
    ) -> dict[str, Any]:
        async with self._auth_lock:
            client = await self._ensure_client(proxy_url=proxy_url)
            if not await self._authorized_within_timeout(client):
                raise TelegramSetupError("Telegram 尚未完成登录")
            try:
                entity = await asyncio.wait_for(
                    client.get_entity(chat_id), timeout=RECONNECT_TIMEOUT
                )
            except Exception as exc:
                raise TelegramSetupError(
                    "无法访问这个频道，请确认 Chat ID 正确，并确认当前账号已加入该私密频道"
                ) from exc
            resolved_start_id = await self._resolve_listener_start(
                client=client,
                chat_id=chat_id,
                start_mode=start_mode,
                start_date=start_date,
                start_message_id=start_message_id,
            )
            previous = next(
                (
                    item
                    for item in self.settings.telegram.channels
                    if item.chat_id == chat_id
                ),
                None,
            )
            start_changed = previous is None or any(
                [
                    previous.start_mode != start_mode,
                    previous.start_date != start_date,
                    (
                        start_mode == "message_id"
                        and previous.start_message_id != resolved_start_id
                    ),
                ]
            )
            channel = ChannelConfig(
                name=(
                    channel_name.strip()
                    or getattr(entity, "title", None)
                    or str(chat_id)
                ),
                chat_id=chat_id,
                enabled=enabled,
                start_message_id=resolved_start_id,
                start_mode=start_mode,
                start_date=start_date,
                grouping_mode=grouping_mode,
                marker_text=marker_text.strip() or "1",
                advertisement_policy=advertisement_policy,
                advertisement_keywords=(
                    advertisement_keywords
                    or ChannelConfig(
                        name="defaults", chat_id=chat_id
                    ).advertisement_keywords
                ),
                display_spacing_hours=display_spacing_hours,
                timeline_mode=timeline_mode,
                display_order=display_order,
                max_concurrent_downloads=max_concurrent_downloads,
            )
            channels = [
                item
                for item in self.settings.telegram.channels
                if item.chat_id != chat_id
            ]
            channels.append(channel)
            listener_enabled = any(item.enabled for item in channels)
            save_telegram_runtime_config(
                enabled=listener_enabled,
                proxy_url=(
                    self.settings.telegram.proxy_url
                    if proxy_url is None
                    else proxy_url
                ),
                channels=channels,
            )
            self.settings.telegram.enabled = listener_enabled
            if proxy_url is not None:
                self.settings.telegram.proxy_url = proxy_url.strip()
            self.settings.telegram.channels = channels
            if start_changed:
                with self.session_factory() as db:
                    row = db.scalar(
                        select(Channel).where(Channel.chat_id == chat_id)
                    )
                    if row:
                        if start_mode == "now":
                            row.last_read_message_id = max(
                                int(row.last_read_message_id or 0),
                                resolved_start_id,
                            )
                        else:
                            row.last_read_message_id = min(
                                int(row.last_read_message_id or resolved_start_id),
                                resolved_start_id,
                            )
                        db.commit()
            if listener_enabled:
                await self._activate_listener()
            start_label = (
                "从现在开始"
                if start_mode == "now"
                else (
                    f"从 {start_date.isoformat()} 开始"
                    if start_mode == "date" and start_date
                    else f"从消息 {resolved_start_id} 开始"
                )
            )
            return {
                "status": self.status,
                "chat_id": chat_id,
                "message": (
                    f"频道“{channel.name}”已保存，{start_label}监听；"
                    "与历史补全重叠的媒体会自动跳过"
                ),
            }

    async def available_channels(self) -> list[dict[str, Any]]:
        async def collect(client: TelegramClient) -> list[dict[str, Any]]:
            """Drain the dialog iterator so the whole sweep can time out."""
            items: list[dict[str, Any]] = []
            async for dialog in client.iter_dialogs():
                entity = dialog.entity
                if not (
                    getattr(entity, "broadcast", False)
                    or getattr(entity, "megagroup", False)
                ):
                    continue
                items.append(
                    {
                        "chat_id": int(dialog.id),
                        "name": (
                            getattr(entity, "title", None)
                            or dialog.name
                            or str(dialog.id)
                        ),
                        "username": getattr(entity, "username", None),
                        "kind": (
                            "channel"
                            if getattr(entity, "broadcast", False)
                            else "group"
                        ),
                    }
                )
            return items

        async with self._auth_lock:
            client = await asyncio.wait_for(
                self._ensure_client(), timeout=RECONNECT_TIMEOUT
            )
            if not await self._authorized_within_timeout(client):
                raise TelegramSetupError("Telegram 尚未完成登录")
            try:
                result = await asyncio.wait_for(
                    collect(client), timeout=CHANNEL_LIST_TIMEOUT
                )
            except TelegramSetupError:
                raise
            except asyncio.TimeoutError as exc:
                self.connection_error = "网络或代理无响应"
                raise TelegramSetupError(
                    "读取频道列表超时：网络或代理无响应，恢复后会自动重连"
                ) from exc
            except Exception as exc:
                raise TelegramSetupError(f"读取频道列表失败：{exc}") from exc
            return sorted(
                result,
                key=lambda item: (str(item["name"]).casefold(), item["chat_id"]),
            )

    async def _resolve_listener_start(
        self,
        *,
        client: TelegramClient,
        chat_id: int,
        start_mode: str,
        start_date: date | None,
        start_message_id: int,
    ) -> int:
        if start_mode not in {"now", "date", "message_id"}:
            raise TelegramSetupError("监听起点方式无效")
        try:
            if start_mode == "now":
                messages = await client.get_messages(chat_id, limit=1)
                return int(messages[0].id) if messages else 0
            if start_mode == "date":
                if start_date is None:
                    raise TelegramSetupError("请选择开始监听日期")
                local_timezone = configured_timezone(self.settings.app.timezone)
                local_start = datetime.combine(
                    start_date,
                    datetime.min.time(),
                    tzinfo=local_timezone,
                )
                messages = await client.get_messages(
                    chat_id,
                    limit=1,
                    offset_date=local_start.astimezone(timezone.utc),
                )
                return int(messages[0].id) if messages else 0
            return max(0, int(start_message_id))
        except TelegramSetupError:
            raise
        except Exception as exc:
            raise TelegramSetupError(f"无法确定监听起点：{exc}") from exc

    async def remove_channel(self, chat_id: int) -> dict[str, Any]:
        async with self._auth_lock:
            channels = [
                item
                for item in self.settings.telegram.channels
                if item.chat_id != chat_id
            ]
            if len(channels) == len(self.settings.telegram.channels):
                raise TelegramSetupError("频道不存在")
            self.settings.telegram.channels = channels
            listener_enabled = any(item.enabled for item in channels)
            self.settings.telegram.enabled = listener_enabled
            save_telegram_runtime_config(
                enabled=listener_enabled,
                proxy_url=self.settings.telegram.proxy_url,
                channels=channels,
            )
            if listener_enabled:
                await self._activate_listener()
            else:
                if self.client and self._handler_registered:
                    self.client.remove_event_handler(self._on_new_message)
                    self.client.remove_event_handler(self._on_album)
                self._handler_registered = False
                self.status = "authorized"
            with self.session_factory() as db:
                row = db.scalar(select(Channel).where(Channel.chat_id == chat_id))
                if row:
                    row.enabled = False
                    db.commit()
            return {"status": self.status, "message": "频道已停止监听，历史文件仍保留"}

    async def update_proxy(self, proxy_url: str) -> dict[str, Any]:
        async with self._auth_lock:
            parsed = proxy_url.strip()
            parse_proxy_url(parsed)
            save_telegram_runtime_config(
                enabled=self.settings.telegram.enabled,
                proxy_url=parsed,
                channels=self.settings.telegram.channels,
            )
            self.settings.telegram.proxy_url = parsed
            client = await asyncio.wait_for(
                self._ensure_client(proxy_url=parsed, force_new=True),
                timeout=RECONNECT_TIMEOUT,
            )
            if (
                await self._authorized_within_timeout(client)
                and self.settings.telegram.channels
            ):
                await self._activate_listener()
            else:
                self.status = "login_required"
            return {"status": self.status, "message": "代理设置已保存并重新连接"}

    async def finalize_subject(self, chat_id: int) -> dict[str, Any]:
        with self.session_factory() as db:
            group = db.scalar(
                select(MediaGroup)
                .where(
                    MediaGroup.chat_id == chat_id,
                    MediaGroup.reason == "channel_marker",
                    MediaGroup.status == "open",
                )
                .order_by(MediaGroup.id.desc())
            )
            if not group:
                raise TelegramSetupError("这个频道没有等待结束的人物批次")
            group.status = "pending"
            group_id = group.id
            db.commit()
        self._schedule_organize(group_id, delay=0)
        return {"status": "pending", "message": "当前人物批次已结束并进入整理队列"}

    async def _ensure_client(
        self,
        *,
        api_id: int | None = None,
        api_hash: str | None = None,
        proxy_url: str | None = None,
        force_new: bool = False,
    ) -> TelegramClient:
        resolved_api_id = api_id or self.settings.telegram_api_id
        resolved_api_hash = api_hash or self.settings.telegram_api_hash
        resolved_proxy = (
            self.settings.telegram.proxy_url if proxy_url is None else proxy_url.strip()
        )
        if not resolved_api_id or not resolved_api_hash:
            raise TelegramSetupError("请先填写 Telegram API ID 和 API Hash")
        identity = (resolved_api_id, resolved_api_hash, resolved_proxy)
        if force_new or (self.client and self._client_identity != identity):
            if self.client:
                await self.client.disconnect()
            self.client = None
            self._handler_registered = False
        if self.client is None:
            session_base = (
                self.settings.storage.session_path
                / self.settings.telegram.session_name
            )
            self.client = TelegramClient(
                str(session_base),
                resolved_api_id,
                resolved_api_hash,
                proxy=parse_proxy_url(resolved_proxy),
            )
            self._client_identity = identity
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def _activate_listener(self) -> None:
        if not self.client:
            raise TelegramSetupError("Telegram 客户端尚未初始化")
        enabled_chat_ids = {
            channel.chat_id
            for channel in self.settings.telegram.channels
            if channel.enabled
        }
        if not enabled_chat_ids:
            raise TelegramSetupError("至少需要启用一个监听频道")
        if self._handler_registered:
            self.client.remove_event_handler(self._on_new_message)
            self.client.remove_event_handler(self._on_album)
        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=list(enabled_chat_ids)),
        )
        self.client.add_event_handler(
            self._on_album,
            events.Album(chats=list(enabled_chat_ids)),
        )
        self._handler_registered = True
        self._sync_channels()
        self.status = "running"
        self._recover_pending_groups()
        self._start_live_queue()
        if not self._watchdog_task or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        logger.info("Telegram 监听已启动，共 %d 个频道", len(enabled_chat_ids))

    def _sync_channels(self) -> None:
        with self.session_factory() as db:
            for channel_config in self.settings.telegram.channels:
                channel = db.scalar(
                    select(Channel).where(Channel.chat_id == channel_config.chat_id)
                )
                if not channel:
                    channel = Channel(
                        chat_id=channel_config.chat_id,
                        name=channel_config.name,
                        enabled=channel_config.enabled,
                        last_read_message_id=channel_config.start_message_id,
                    )
                    db.add(channel)
                else:
                    channel.name = channel_config.name
                    channel.enabled = channel_config.enabled
                db.commit()

    async def _on_new_message(self, event: Any) -> None:
        message = event.message
        if not message or not message.media:
            return
        # Album messages are queued together by the Album event.
        if getattr(message, "grouped_id", None):
            return
        self._enqueue_live_message(int(event.chat_id), message)

    async def _on_album(self, event: Any) -> None:
        channel_config = self._channel_config(int(event.chat_id))
        force_advertisement = bool(
            channel_config
            and any(
                is_advertisement_caption(
                    getattr(message, "message", None),
                    channel_config.advertisement_keywords,
                )
                for message in event.messages
            )
        )
        for message in event.messages:
            self._enqueue_live_message(
                int(event.chat_id),
                message,
                force_advertisement=force_advertisement,
            )

    def _ensure_live_control(self, db: Session) -> Task:
        control = db.scalar(
            select(Task)
            .where(Task.kind == "live_listener_control")
            .order_by(Task.id.desc())
        )
        if control:
            return control
        control = Task(
            kind="live_listener_control",
            status="running",
            progress=0,
            payload_json="{}",
        )
        db.add(control)
        db.flush()
        return control

    def _start_live_queue(self) -> None:
        with self.session_factory() as db:
            db.execute(
                update(LiveDownloadItem)
                .where(LiveDownloadItem.status == "downloading")
                .values(status="queued", received_bytes=0)
            )
            control = self._ensure_live_control(db)
            self._live_paused = control.status == "paused"
            db.commit()
        if not self._live_dispatcher or self._live_dispatcher.done():
            self._live_dispatcher = asyncio.create_task(self._live_dispatch_loop())
        if not self._live_catchup_task or self._live_catchup_task.done():
            self._live_catchup_task = asyncio.create_task(
                self._catch_up_enabled_channels()
            )
        for channel in self.settings.telegram.channels:
            if channel.enabled and channel.grouping_mode == "marker":
                self._schedule_subject_idle_finalize(channel.chat_id, delay=5)
                if self.channel_reconcile_callback is not None:
                    self.channel_reconcile_callback(channel.chat_id, delay=8)
        self._live_wakeup.set()

    async def _stop_live_queue(self) -> None:
        tasks = [
            task
            for task in [
                self._live_dispatcher,
                self._live_catchup_task,
                *self._live_workers.values(),
            ]
            if task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._live_dispatcher = None
        self._live_catchup_task = None
        self._live_workers.clear()
        self._live_active.clear()
        with self.session_factory() as db:
            db.execute(
                update(LiveDownloadItem)
                .where(LiveDownloadItem.status == "downloading")
                .values(status="queued", received_bytes=0)
            )
            db.commit()

    def _enqueue_live_message(
        self,
        chat_id: int,
        message: Any,
        *,
        force_advertisement: bool = False,
    ) -> bool:
        channel_config = self._channel_config(chat_id)
        if not channel_config or not getattr(message, "media", None):
            return False
        idle_task = self._subject_idle_tasks.pop(int(chat_id), None)
        if idle_task and not idle_task.done():
            idle_task.cancel()
        message_id = int(message.id)
        with self.session_factory() as db:
            already_downloaded = db.scalar(
                select(TelegramMessage.id).where(
                    TelegramMessage.chat_id == chat_id,
                    TelegramMessage.message_id == message_id,
                )
            )
            if already_downloaded:
                return False
            item = db.scalar(
                select(LiveDownloadItem).where(
                    LiveDownloadItem.chat_id == chat_id,
                    LiveDownloadItem.message_id == message_id,
                )
            )
            if item:
                if item.status == "failed":
                    item.status = "queued"
                    item.error = None
                    item.attempts = 0
                    db.commit()
                    self._live_wakeup.set()
                return False
            db.add(
                LiveDownloadItem(
                    chat_id=chat_id,
                    message_id=message_id,
                    media_group_id=(
                        str(message.grouped_id)
                        if getattr(message, "grouped_id", None)
                        else None
                    ),
                    original_filename=self._original_filename(message),
                    force_advertisement=force_advertisement,
                    status="queued",
                )
            )
            db.commit()
        self._live_wakeup.set()
        return True

    async def _catch_up_enabled_channels(self) -> None:
        """Queue media published while the container was stopped."""
        try:
            if not self.client:
                return
            for channel_config in self.settings.telegram.channels:
                if not channel_config.enabled:
                    continue
                with self.session_factory() as db:
                    channel = db.scalar(
                        select(Channel).where(
                            Channel.chat_id == channel_config.chat_id
                        )
                    )
                    minimum_id = (
                        int(channel.last_read_message_id)
                        if channel
                        else int(channel_config.start_message_id)
                    )
                async for message in self.client.iter_messages(
                    channel_config.chat_id,
                    min_id=minimum_id,
                    reverse=True,
                ):
                    if not getattr(message, "media", None):
                        continue
                    self._enqueue_live_message(
                        channel_config.chat_id,
                        message,
                        force_advertisement=is_advertisement_caption(
                            getattr(message, "message", None),
                            channel_config.advertisement_keywords,
                        ),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("实时监听断点补取失败，现有下载队列仍会继续")

    async def _live_dispatch_loop(self) -> None:
        try:
            while True:
                if self._live_paused or self.status != "running":
                    self._live_wakeup.clear()
                    try:
                        await asyncio.wait_for(self._live_wakeup.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue

                active_per_chat: dict[int, int] = {}
                for active in self._live_active.values():
                    chat_id = int(active["chat_id"])
                    active_per_chat[chat_id] = active_per_chat.get(chat_id, 0) + 1

                selected_items: list[tuple[int, int, int, str]] = []
                with self.session_factory() as db:
                    queued = list(
                        db.scalars(
                            select(LiveDownloadItem)
                            .where(LiveDownloadItem.status == "queued")
                            .order_by(
                                LiveDownloadItem.message_id,
                                LiveDownloadItem.id,
                            )
                            .limit(200)
                        )
                    )
                    for item in queued:
                        channel = self._channel_config(int(item.chat_id))
                        if not channel:
                            continue
                        current = active_per_chat.get(int(item.chat_id), 0)
                        limit = max(
                            1,
                            min(8, int(channel.max_concurrent_downloads)),
                        )
                        if current >= limit:
                            continue
                        item.status = "downloading"
                        item.started_at = datetime.now(timezone.utc)
                        item.error = None
                        item.received_bytes = 0
                        active_per_chat[int(item.chat_id)] = current + 1
                        selected_items.append(
                            (
                                int(item.id),
                                int(item.chat_id),
                                int(item.message_id),
                                item.original_filename or f"消息 {item.message_id}",
                            )
                        )
                    db.commit()

                for item_id, chat_id, message_id, filename in selected_items:
                    self._live_active[item_id] = {
                        "item_id": item_id,
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "filename": filename,
                        "received": 0,
                        "total": 0,
                        "speed_bps": 0,
                    }
                    worker = asyncio.create_task(self._process_live_item(item_id))
                    self._live_workers[item_id] = worker
                    worker.add_done_callback(
                        lambda completed, current_id=item_id: self._forget_live_worker(
                            current_id, completed
                        )
                    )

                if not selected_items:
                    self._live_wakeup.clear()
                    try:
                        await asyncio.wait_for(self._live_wakeup.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise

    def _forget_live_worker(
        self, item_id: int, completed: asyncio.Task
    ) -> None:
        if self._live_workers.get(item_id) is completed:
            self._live_workers.pop(item_id, None)
        self._live_active.pop(item_id, None)
        with suppress(asyncio.CancelledError):
            error = completed.exception()
            if error:
                logger.error("实时下载工作线程异常：%s", error)
        self._live_wakeup.set()

    async def _respect_live_cooldown(self) -> None:
        remaining = self._live_cooldown_until - time.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

    def media_download_lock(
        self, chat_id: int, message_id: int
    ) -> asyncio.Lock:
        """One in-process lock shared by live listening and history rebuilds."""
        key = (int(chat_id), int(message_id))
        lock = self._media_download_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._media_download_locks[key] = lock
        return lock

    async def _process_live_item(self, item_id: int) -> None:
        with self.session_factory() as db:
            queued_item = db.get(LiveDownloadItem, item_id)
            if not queued_item:
                return
            lock = self.media_download_lock(
                int(queued_item.chat_id), int(queued_item.message_id)
            )
        async with lock:
            await self._process_live_item_locked(item_id)

    async def _process_live_item_locked(self, item_id: int) -> None:
        partial: Path | None = None
        try:
            with self.session_factory() as db:
                item = db.get(LiveDownloadItem, item_id)
                if not item:
                    return
                chat_id = int(item.chat_id)
                message_id = int(item.message_id)
                force_advertisement = bool(item.force_advertisement)
                item.attempts += 1
                attempt = int(item.attempts)
                db.commit()

                existing = db.scalar(
                    select(TelegramMessage).where(
                        TelegramMessage.chat_id == chat_id,
                        TelegramMessage.message_id == message_id,
                    )
                )
                if existing:
                    item.status = "completed"
                    item.finished_at = datetime.now(timezone.utc)
                    db.commit()
                    return
                rebuilt = db.scalar(
                    select(RebuildItem).where(
                        RebuildItem.chat_id == chat_id,
                        RebuildItem.message_id == message_id,
                    )
                )
                if rebuilt and Path(rebuilt.source_path).is_file():
                    item.status = "completed"
                    item.received_bytes = int(rebuilt.size_bytes or 0)
                    item.total_bytes = int(rebuilt.size_bytes or 0)
                    item.error = None
                    item.finished_at = datetime.now(timezone.utc)
                    channel_row = db.scalar(
                        select(Channel).where(Channel.chat_id == chat_id)
                    )
                    if channel_row:
                        channel_row.last_read_message_id = max(
                            int(channel_row.last_read_message_id or 0),
                            message_id,
                        )
                    db.commit()
                    return

            channel_config = self._channel_config(chat_id)
            if not channel_config:
                raise RuntimeError("频道已停止监听")
            client = await self._ensure_client()
            await self._respect_live_cooldown()
            message = await client.get_messages(chat_id, ids=message_id)
            if not message or not getattr(message, "media", None):
                raise RuntimeError(f"消息 {message_id} 不存在或已不再包含媒体")
            if getattr(message, "grouped_id", None):
                nearby = await client.get_messages(
                    chat_id,
                    ids=list(range(max(1, message_id - 12), message_id + 13)),
                )
                for sibling in nearby or []:
                    if (
                        sibling
                        and getattr(sibling, "media", None)
                        and getattr(sibling, "grouped_id", None)
                        == message.grouped_id
                    ):
                        self._enqueue_live_message(
                            chat_id,
                            sibling,
                            force_advertisement=force_advertisement,
                        )

            original_name = self._original_filename(message)
            month = local_month(message.date, self.settings.app.timezone)
            channel_dir = (
                self.settings.storage.download_path
                / self._safe_channel_name(channel_config)
                / month
            )
            channel_dir.mkdir(parents=True, exist_ok=True)
            saved_name = self._raw_saved_filename(
                message, original_name, channel_config.marker_text
            )
            destination = channel_dir / saved_name
            partial = destination.with_name(f".{destination.name}.live.part")

            received_before = 0
            started = time.monotonic()
            last_saved = [0.0]

            def report_download(received: int, total_bytes: int) -> None:
                nonlocal received_before
                now = time.monotonic()
                received_value = int(received)
                received_before = max(received_before, received_value)
                active = {
                    "item_id": item_id,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "filename": saved_name,
                    "received": received_value,
                    "total": int(total_bytes or 0),
                    "speed_bps": received_value / max(now - started, 0.001),
                }
                self._live_active[item_id] = active
                if now - last_saved[0] >= 1.0 or received >= total_bytes:
                    last_saved[0] = now
                    with self.session_factory() as progress_db:
                        progress_db.execute(
                            update(LiveDownloadItem)
                            .where(LiveDownloadItem.id == item_id)
                            .values(
                                received_bytes=received_value,
                                total_bytes=int(total_bytes or 0),
                            )
                        )
                        progress_db.commit()

            if not destination.is_file():
                for download_attempt in range(1, 6):
                    try:
                        await self._respect_live_cooldown()
                        if partial.exists():
                            partial.unlink()
                        message = await client.get_messages(chat_id, ids=message_id)
                        if not message or not getattr(message, "media", None):
                            raise RuntimeError(
                                f"消息 {message_id} 不存在或已不再包含媒体"
                            )
                        await message.download_media(
                            file=str(partial),
                            progress_callback=report_download,
                        )
                        break
                    except FloodWaitError as exc:
                        self._live_cooldown_until = max(
                            self._live_cooldown_until,
                            time.time() + max(1, int(exc.seconds)) + 1,
                        )
                        await self._respect_live_cooldown()
                    except FileReferenceExpiredError:
                        if download_attempt >= 5:
                            raise
                    except (
                        TimedOutError,
                        RpcCallFailError,
                        ServerError,
                        OSError,
                    ):
                        if download_attempt >= 5:
                            raise
                        await asyncio.sleep(min(30, 2 ** download_attempt))
                if not partial.is_file():
                    raise RuntimeError(f"消息 {message_id} 下载后未找到临时文件")
                os.replace(partial, destination)

            if not destination.is_file():
                raise RuntimeError(f"消息 {message_id} 下载后未找到文件")
            size_bytes = int(destination.stat().st_size)
            self._live_session_bytes += size_bytes
            _group_id, ready_group_ids = self._record_download(
                channel_config,
                message,
                destination,
                original_name,
                force_advertisement=force_advertisement,
            )
            if self.channel_reconcile_callback is not None:
                self.channel_reconcile_callback(chat_id, delay=300)
            self._schedule_subject_idle_finalize(chat_id)
            telegram_group_ready = True
            with self.session_factory() as db:
                item = db.get(LiveDownloadItem, item_id)
                if item:
                    item.status = "completed"
                    item.received_bytes = size_bytes
                    item.total_bytes = size_bytes
                    item.error = None
                    item.finished_at = datetime.now(timezone.utc)
                    db.commit()
                if getattr(message, "grouped_id", None):
                    telegram_group_ready = not bool(
                        db.scalar(
                            select(func.count())
                            .select_from(LiveDownloadItem)
                            .where(
                                LiveDownloadItem.chat_id == chat_id,
                                LiveDownloadItem.media_group_id
                                == str(message.grouped_id),
                                LiveDownloadItem.status.in_(
                                    ["queued", "downloading"]
                                ),
                            )
                        )
                    )
            if telegram_group_ready:
                for group_id in ready_group_ids:
                    self._schedule_organize(group_id, delay=1.5)
        except asyncio.CancelledError:
            with self.session_factory() as db:
                item = db.get(LiveDownloadItem, item_id)
                if item and item.status == "downloading":
                    item.status = "queued"
                    item.received_bytes = 0
                    db.commit()
            raise
        except Exception as exc:
            logger.exception("实时监听消息 %s 下载失败", item_id)
            with self.session_factory() as db:
                item = db.get(LiveDownloadItem, item_id)
                if item:
                    item.error = str(exc)
                    item.received_bytes = 0
                    item.status = "queued" if int(item.attempts) < 5 else "failed"
                    if item.status == "failed":
                        item.finished_at = datetime.now(timezone.utc)
                    db.commit()
            if attempt < 5 and not self._live_paused:
                await asyncio.sleep(min(30, 2 ** attempt))
        finally:
            self._live_active.pop(item_id, None)
            if partial and partial.exists():
                partial.unlink()
            self._live_wakeup.set()

    async def pause_live_downloads(self) -> dict[str, Any]:
        self._live_paused = True
        with self.session_factory() as db:
            control = self._ensure_live_control(db)
            control.status = "paused"
            db.commit()
        workers = [
            task for task in self._live_workers.values() if not task.done()
        ]
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        with self.session_factory() as db:
            db.execute(
                update(LiveDownloadItem)
                .where(LiveDownloadItem.status == "downloading")
                .values(status="queued", received_bytes=0)
            )
            db.commit()
        self._live_wakeup.set()
        return {
            "status": "paused",
            "message": "实时监听下载已暂停；新消息仍会进入队列，不会丢失",
        }

    async def resume_live_downloads(self) -> dict[str, Any]:
        with self.session_factory() as db:
            control = self._ensure_live_control(db)
            control.status = "running"
            db.execute(
                update(LiveDownloadItem)
                .where(LiveDownloadItem.status == "failed")
                .values(status="queued", attempts=0, error=None)
            )
            db.commit()
        self._live_paused = False
        if not self._live_dispatcher or self._live_dispatcher.done():
            self._live_dispatcher = asyncio.create_task(self._live_dispatch_loop())
        self._live_wakeup.set()
        return {"status": "running", "message": "实时监听下载已继续"}

    def live_download_status(self) -> dict[str, Any]:
        with self.session_factory() as db:
            control = self._ensure_live_control(db)
            db.commit()
            counts = {
                status: int(
                    db.scalar(
                        select(func.count())
                        .select_from(LiveDownloadItem)
                        .where(LiveDownloadItem.status == status)
                    )
                    or 0
                )
                for status in ["queued", "downloading", "completed", "failed"]
            }
            channel_rows = []
            for channel in sorted(
                self.settings.telegram.channels,
                key=lambda item: item.display_order,
            ):
                channel_counts = {
                    status: int(
                        db.scalar(
                            select(func.count())
                            .select_from(LiveDownloadItem)
                            .where(
                                LiveDownloadItem.chat_id == channel.chat_id,
                                LiveDownloadItem.status == status,
                            )
                        )
                        or 0
                    )
                    for status in [
                        "queued",
                        "downloading",
                        "completed",
                        "failed",
                    ]
                }
                channel_rows.append(
                    {
                        "chat_id": channel.chat_id,
                        "name": channel.name,
                        "enabled": channel.enabled,
                        "max_concurrent_downloads": (
                            channel.max_concurrent_downloads
                        ),
                        **channel_counts,
                    }
                )
        active_downloads = sorted(
            self._live_active.values(),
            key=lambda item: (int(item["chat_id"]), int(item["message_id"])),
        )
        speed_bps = sum(
            float(item.get("speed_bps") or 0) for item in active_downloads
        )
        return {
            "status": (
                "paused"
                if self._live_paused or control.status == "paused"
                else self.status
            ),
            "paused": self._live_paused or control.status == "paused",
            "pending": counts["queued"] + counts["downloading"],
            "speed_bps": speed_bps,
            "session_bytes": self._live_session_bytes,
            "session_seconds": max(
                0, int(time.monotonic() - self._live_session_started)
            ),
            "active_downloads": active_downloads,
            "channels": channel_rows,
            **counts,
        }

    async def _download_message(
        self,
        chat_id: int,
        message: Any,
        *,
        force_advertisement: bool = False,
    ) -> list[int]:
        channel_config = self._channel_config(chat_id)
        if not channel_config:
            return []
        with self.session_factory() as db:
            exists = db.scalar(
                select(TelegramMessage).where(
                    TelegramMessage.chat_id == chat_id,
                    TelegramMessage.message_id == int(message.id),
                )
            )
            if exists:
                if not exists.files or not exists.files[0].group_id:
                    return []
                existing_group = db.get(MediaGroup, exists.files[0].group_id)
                if existing_group and existing_group.status in {"open", "excluded"}:
                    return []
                return [exists.files[0].group_id]

        original_name = self._original_filename(message)
        month = local_month(message.date, self.settings.app.timezone)
        channel_dir = (
            self.settings.storage.download_path
            / self._safe_channel_name(channel_config)
            / month
        )
        channel_dir.mkdir(parents=True, exist_ok=True)
        saved_name = self._raw_saved_filename(
            message, original_name, channel_config.marker_text
        )
        destination = channel_dir / saved_name
        try:
            await message.download_media(file=str(destination))
            _group_id, ready_group_ids = self._record_download(
                channel_config,
                message,
                destination,
                original_name,
                force_advertisement=force_advertisement,
            )
            return ready_group_ids
        except Exception:
            logger.exception("Telegram 消息 %s 下载失败", message.id)
            return []

    def _schedule_organize(self, group_id: int, delay: float = 2.0) -> None:
        if not (
            self.settings.organizer.enabled
            and self.settings.organizer.auto_organize
        ):
            return
        previous = self._organize_tasks.get(group_id)
        if previous and not previous.done():
            previous.cancel()
        task = asyncio.create_task(self._organize_after_delay(group_id, delay))
        self._organize_tasks[group_id] = task
        task.add_done_callback(
            lambda completed, current_group_id=group_id: self._forget_organize_task(
                current_group_id, completed
            )
        )

    def _schedule_subject_idle_finalize(
        self, chat_id: int, delay: float = 300.0
    ) -> None:
        """Seal an open marker subject after the channel has been idle.

        The timer is reset for every newly queued media item.  This lets a
        person span several Telegram albums, while still finalizing the last
        person when a channel stops publishing without sending the next
        marker.
        """
        chat_id = int(chat_id)
        previous = self._subject_idle_tasks.get(chat_id)
        if previous and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            self._finalize_subject_after_idle(chat_id, max(1.0, delay))
        )
        self._subject_idle_tasks[chat_id] = task
        task.add_done_callback(
            lambda completed, current_chat_id=chat_id: (
                self._subject_idle_tasks.pop(current_chat_id, None)
                if self._subject_idle_tasks.get(current_chat_id) is completed
                else None
            )
        )

    async def _finalize_subject_after_idle(
        self, chat_id: int, initial_delay: float
    ) -> None:
        try:
            await asyncio.sleep(initial_delay)
            while True:
                with self.session_factory() as db:
                    open_group = db.scalar(
                        select(MediaGroup)
                        .where(
                            MediaGroup.chat_id == chat_id,
                            MediaGroup.reason == "channel_marker",
                            MediaGroup.status == "open",
                        )
                        .order_by(MediaGroup.id.desc())
                    )
                    if not open_group:
                        return
                    pending = int(
                        db.scalar(
                            select(func.count())
                            .select_from(LiveDownloadItem)
                            .where(
                                LiveDownloadItem.chat_id == chat_id,
                                LiveDownloadItem.status.in_(
                                    ["queued", "downloading"]
                                ),
                            )
                        )
                        or 0
                    )
                    latest = db.scalar(
                        select(func.max(TelegramMessage.downloaded_at))
                        .join(
                            MediaFile,
                            MediaFile.message_id_fk == TelegramMessage.id,
                        )
                        .where(MediaFile.group_id == open_group.id)
                    )
                    if pending:
                        wait_seconds = 30.0
                    else:
                        if latest is None:
                            latest = open_group.created_at
                        if latest.tzinfo is None:
                            latest = latest.replace(tzinfo=timezone.utc)
                        age = (
                            datetime.now(timezone.utc) - latest.astimezone(timezone.utc)
                        ).total_seconds()
                        wait_seconds = max(0.0, 300.0 - age)
                        if wait_seconds <= 0:
                            open_group.status = "pending"
                            db.commit()
                            if self.channel_reconcile_callback is not None:
                                self.channel_reconcile_callback(
                                    chat_id,
                                    delay=0,
                                    refresh_metadata=True,
                                )
                            return
                await asyncio.sleep(max(1.0, wait_seconds))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("频道 %s 空闲自动归档人物失败", chat_id)

    async def organize_now(self, group_id: int) -> MediaGroup:
        scheduled = self._organize_tasks.pop(group_id, None)
        if scheduled and not scheduled.done():
            scheduled.cancel()
            with suppress(asyncio.CancelledError):
                await scheduled
        with self.session_factory() as db:
            group = organize_group(db, self.settings, group_id)
        if group.status == "organized" and self.immich is not None:
            self.immich.schedule_refresh()
        return group

    def _forget_organize_task(
        self, group_id: int, completed: asyncio.Task
    ) -> None:
        if self._organize_tasks.get(group_id) is completed:
            self._organize_tasks.pop(group_id, None)

    async def _organize_after_delay(self, group_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            with self.session_factory() as db:
                group = db.get(MediaGroup, group_id)
                if not group or group.status == "organized":
                    return
                group.status = "organizing"
                db.commit()
                organized = organize_group(db, self.settings, group_id)
                if (
                    organized.status == "organized"
                    and self.immich is not None
                ):
                    self.immich.schedule_refresh()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("资源组 %s 自动整理失败", group_id)
            with self.session_factory() as db:
                group = db.get(MediaGroup, group_id)
                if group:
                    group.status = "error"
                    db.commit()

    def _recover_pending_groups(self) -> None:
        with self.session_factory() as db:
            group_ids = list(
                db.scalars(
                    select(MediaGroup.id).where(
                        MediaGroup.status.in_(["pending", "organizing"])
                    )
                )
            )
        for group_id in group_ids:
            self._schedule_organize(group_id, delay=2.0)

    def _record_download(
        self,
        channel_config: ChannelConfig,
        message: Any,
        destination: Path,
        original_name: str,
        *,
        force_advertisement: bool = False,
    ) -> tuple[int, list[int]]:
        metadata = parse_filename(original_name)
        with self.session_factory() as db:
            channel = db.scalar(
                select(Channel).where(Channel.chat_id == channel_config.chat_id)
            )
            if not channel:
                raise RuntimeError("频道记录不存在")
            telegram_message = TelegramMessage(
                channel_id=channel.id,
                chat_id=channel.chat_id,
                message_id=int(message.id),
                media_group_id=(
                    str(message.grouped_id)
                    if getattr(message, "grouped_id", None)
                    else None
                ),
                telegram_date=message.date,
                caption=getattr(message, "message", None),
                downloaded_at=datetime.now(timezone.utc),
            )
            db.add(telegram_message)
            db.flush()
            group, ready_group_ids = self._find_or_create_group(
                db,
                telegram_message,
                channel_config,
                force_advertisement=force_advertisement,
            )
            db.add(
                MediaFile(
                    message_id_fk=telegram_message.id,
                    group_id=group.id,
                    original_path=str(destination),
                    original_filename=original_name,
                    # Keep the logical name stable for the整理库.  The raw
                    # The archive may contain the visual cover annotation.
                    saved_filename=self._logical_saved_filename(
                        message, original_name, channel_config.marker_text
                    ),
                    mime_type=mimetypes.guess_type(original_name)[0],
                    size_bytes=destination.stat().st_size,
                    camera_prefix=metadata.camera_prefix,
                    camera_index=metadata.camera_index,
                    original_order=int(message.id),
                    display_order=int(message.id),
                )
            )
            channel.last_read_message_id = max(
                channel.last_read_message_id, int(message.id)
            )
            group.start_message_id = min(group.start_message_id, int(message.id))
            group.end_message_id = max(group.end_message_id, int(message.id))
            db.commit()
            return group.id, ready_group_ids

    def _find_or_create_group(
        self,
        db: Session,
        message: TelegramMessage,
        channel_config: ChannelConfig,
        *,
        force_advertisement: bool = False,
    ) -> tuple[MediaGroup, list[int]]:
        if channel_config.grouping_mode == "marker":
            is_marker = is_marker_caption(
                message.caption, channel_config.marker_text
            )
            is_advertisement = force_advertisement or is_advertisement_caption(
                message.caption,
                channel_config.advertisement_keywords,
            )
            if is_marker:
                previous = db.scalar(
                    select(MediaGroup)
                    .where(
                        MediaGroup.chat_id == message.chat_id,
                        MediaGroup.reason == "channel_marker",
                        MediaGroup.status == "open",
                    )
                    .order_by(MediaGroup.id.desc())
                )
                ready = []
                if previous:
                    previous.status = "pending"
                    ready.append(previous.id)
                group = self._new_group(
                    db,
                    message,
                    reason="channel_marker",
                    confidence=0.99,
                    identity=str(message.message_id),
                    status="open",
                )
                return group, ready
            if is_advertisement:
                group = self._group_for_telegram_unit(
                    db,
                    message,
                    reason="advertisement",
                    confidence=0.92,
                )
                if channel_config.advertisement_policy == "keep":
                    group.status = "excluded"
                    return group, []
                return group, [group.id]
            subject = db.scalar(
                select(MediaGroup)
                .where(
                    MediaGroup.chat_id == message.chat_id,
                    MediaGroup.reason == "channel_marker",
                    MediaGroup.status == "open",
                )
                .order_by(MediaGroup.id.desc())
            )
            if subject:
                return subject, []

        group = self._group_for_telegram_unit(
            db,
            message,
            reason=(
                "telegram_media_group"
                if message.media_group_id
                else "telegram_single_message"
            ),
            confidence=1.0,
        )
        return group, [group.id]

    def _group_for_telegram_unit(
        self,
        db: Session,
        message: TelegramMessage,
        *,
        reason: str,
        confidence: float,
    ) -> MediaGroup:
        if message.media_group_id:
            group = db.scalar(
                select(MediaGroup).where(
                    MediaGroup.chat_id == message.chat_id,
                    MediaGroup.telegram_media_group_id == message.media_group_id,
                    MediaGroup.reason == reason,
                )
            )
            if group:
                return group
            identity = message.media_group_id[-12:]
        else:
            identity = str(message.message_id)
        return self._new_group(
            db,
            message,
            reason=reason,
            confidence=confidence,
            identity=identity,
        )

    @staticmethod
    def _new_group(
        db: Session,
        message: TelegramMessage,
        *,
        reason: str,
        confidence: float,
        identity: str,
        status: str = "pending",
    ) -> MediaGroup:
        group = MediaGroup(
            public_id=(
                f"{'S' if reason == 'channel_marker' else 'A' if reason == 'advertisement' else 'G'}"
                f"{abs(message.chat_id) % 1_000_000:06d}_{identity}"
            )[:32],
            chat_id=message.chat_id,
            telegram_media_group_id=message.media_group_id,
            title=(message.caption or "")[:255] or None,
            start_message_id=message.message_id,
            end_message_id=message.message_id,
            confidence=confidence,
            reason=reason,
            status=status,
        )
        db.add(group)
        db.flush()
        return group

    def _channel_config(self, chat_id: int) -> ChannelConfig | None:
        return next(
            (
                item
                for item in self.settings.telegram.channels
                if item.enabled and item.chat_id == chat_id
            ),
            None,
        )

    @staticmethod
    def _safe_channel_name(channel: ChannelConfig) -> str:
        """Use immutable Chat ID for live and history physical directories."""
        return str(int(channel.chat_id))

    @staticmethod
    def _safe_filename(filename: str) -> str:
        safe = UNSAFE_FILENAME_RE.sub("_", filename).strip(" .")
        return safe[:240] or "unnamed-media.bin"

    @classmethod
    def _logical_saved_filename(
        cls, message: Any, original_name: str, marker_text: str = "1"
    ) -> str:
        safe_original = cls._safe_filename(original_name)
        if is_marker_caption(getattr(message, "message", None), marker_text):
            return f"{message.id} - {COVER_FILENAME_TAG} {safe_original}"
        return f"{message.id} - {safe_original}"

    @classmethod
    def _raw_saved_filename(
        cls, message: Any, original_name: str, marker_text: str = "1"
    ) -> str:
        """Return the raw filename; an exact marker is annotated once."""
        return cls._logical_saved_filename(message, original_name, marker_text)

    @staticmethod
    def _original_filename(message: Any) -> str:
        document = getattr(message, "document", None)
        if document:
            for attribute in document.attributes:
                if isinstance(attribute, DocumentAttributeFilename):
                    return attribute.file_name
            # In-app recorded and forwarded videos often carry no filename
            # attribute. A .bin fallback is never importable by Immich, so the
            # media type is derived from the document's own MIME type instead.
            extension = MIME_EXTENSION_OVERRIDES.get(
                str(getattr(document, "mime_type", "") or "").lower().strip()
            )
            if not extension:
                guessed = mimetypes.guess_extension(
                    str(getattr(document, "mime_type", "") or "").lower().strip()
                    or "application/octet-stream"
                )
                extension = (
                    guessed
                    if guessed and guessed.lower() in IMMICH_MEDIA_EXTENSIONS
                    else None
                )
            if extension:
                return f"telegram_{message.id}{extension}"
        extension = ".jpg" if getattr(message, "photo", None) else ".bin"
        return f"telegram_{message.id}{extension}"

    # ------------------------------------------------------------------
    # 网络看门狗：检测代理/网络中断并在恢复后自动重连
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Periodically verify both clients and reconnect after outages."""
        while True:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                # Only heal a listener that was actually running. Login and
                # first-time setup states (code_sent / password_required /
                # authorized) must not be touched mid-flow.
                if self.status not in {"running", "connection_error"}:
                    continue
                await self._watchdog_check_client()
                await self._watchdog_check_bot()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("网络看门狗执行异常，稍后重试")

    async def _watchdog_check_client(self) -> None:
        """Probe the listener client; mark and heal connection failures."""
        if not self.client:
            return
        healthy = False
        detail = ""
        try:
            if not self._handler_registered:
                # A rebuild that died before _activate_listener leaves a
                # connected client with no handlers: it would look healthy
                # while silently receiving nothing.
                detail = "监听未完成注册"
            elif self.client.is_connected():
                healthy = bool(
                    await asyncio.wait_for(
                        self.client.is_user_authorized(), timeout=WATCHDOG_PROBE_TIMEOUT
                    )
                )
                if not healthy:
                    detail = "会话已失效，需要重新登录"
            else:
                detail = "客户端连接已断开"
        except asyncio.TimeoutError:
            detail = "网络或代理无响应"
        except AuthKeyError:
            self.status = "login_required"
            self.connection_error = "会话密钥失效，请重新登录"
            logger.warning("Telegram 会话密钥失效，需要重新登录")
            return
        except Exception as exc:  # pragma: no cover - defensive
            detail = str(exc) or exc.__class__.__name__
        if healthy:
            if self.status == "connection_error":
                logger.info("Telegram 监听连接已自行恢复")
            self.status = "running"
            self.connection_error = ""
            return
        self.connection_error = detail or "连接异常"
        if self.status != "connection_error":
            logger.warning("Telegram 监听连接异常：%s", self.connection_error)
        self.status = "connection_error"
        await self._attempt_reconnect()

    async def _watchdog_check_bot(self) -> None:
        """Probe the forward bot and rebuild it after an outage.

        A dead proxy stalls in-flight forward downloads instead of finishing
        them, so the bot must be rebuilt regardless of pending tasks. Stalled
        tasks are cancelled first; each worker rewinds its own rows to
        ``queued`` on cancellation, and ``_recover_forward_tasks`` relaunches
        them once the fresh bot client is online.
        """
        config = self.settings.telegram
        if not config.bot_enabled or not self.settings.telegram_bot_token:
            self.bot_error = ""
            return
        healthy = False
        detail = ""
        if not self.bot_client:
            detail = "机器人未启动"
        elif not self._bot_ready:
            # A previous reconnect died midway through start(): the client can
            # answer probes but has no handlers and never resumed its queue.
            detail = "机器人上次重连未完成"
        else:
            try:
                await asyncio.wait_for(
                    self.bot_client.get_me(), timeout=WATCHDOG_PROBE_TIMEOUT
                )
                healthy = self.bot_client.is_connected()
                if not healthy:
                    detail = "机器人连接已断开"
            except asyncio.TimeoutError:
                detail = "机器人网络或代理无响应"
            except Exception as exc:  # pragma: no cover - defensive
                detail = str(exc) or exc.__class__.__name__
        if healthy:
            if self.bot_error:
                logger.info("Telegram 机器人连接已自行恢复")
            self.bot_error = ""
            return
        self.bot_error = detail or "机器人连接异常"
        logger.warning("Telegram 机器人连接异常：%s，准备重连", self.bot_error)
        await self._cancel_stalled_forward_tasks()
        await self._rebuild_bot_client()

    async def _cancel_stalled_forward_tasks(self) -> None:
        """Cancel forward tasks pinned to the dead bot client."""
        pending = [task for task in self._forward_tasks.values() if not task.done()]
        if not pending:
            return
        logger.info("取消 %d 个卡在断网状态的转发任务", len(pending))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._forward_tasks.clear()
        self._forward_active.clear()

    @staticmethod
    async def _authorized_within_timeout(client: TelegramClient) -> bool:
        """is_user_authorized() that fails fast instead of hanging the request.

        Telethon retries indefinitely behind a dead proxy, which used to freeze
        every setup page that waited on this call.
        """
        try:
            return bool(
                await asyncio.wait_for(
                    client.is_user_authorized(), timeout=WATCHDOG_PROBE_TIMEOUT
                )
            )
        except asyncio.TimeoutError as exc:
            raise TelegramSetupError(
                "网络或代理无响应，请检查代理后重试；恢复后序影会自动重连"
            ) from exc

    @staticmethod
    async def _safe_disconnect(client: TelegramClient | None) -> None:
        """Drop a client without letting a dead socket block the caller."""
        if not client:
            return
        with suppress(Exception):
            await asyncio.wait_for(
                client.disconnect(), timeout=WATCHDOG_PROBE_TIMEOUT
            )

    async def _reconnect_bot_client(self) -> bool:
        """Drop stalled tasks and bring up a fresh bot client."""
        await self._cancel_stalled_forward_tasks()
        if self.bot_client:
            await self._safe_disconnect(self.bot_client)
            self.bot_client = None
        try:
            # A still-dead proxy would make start() hang forever and stall the
            # watchdog; bound it so the next cycle can retry cleanly.
            await asyncio.wait_for(self._start_bot(), timeout=RECONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            self._bot_ready = False
            self.bot_error = "机器人重连超时，网络或代理仍不可用"
            logger.warning("Telegram 机器人重连超时，将在下次检查重试")
            return False
        except Exception as exc:
            self._bot_ready = False
            self.bot_error = str(exc) or exc.__class__.__name__
            logger.warning(
                "Telegram 机器人重连失败：%s，将在下次检查重试", self.bot_error
            )
            return False
        self.bot_error = ""
        logger.info("Telegram 机器人已重连")
        return True

    async def _rebuild_bot_client(self) -> None:
        """Watchdog path: reconnect and resume queued forward tasks.

        ``_start_bot`` ends with ``_recover_forward_tasks``, so the tasks that
        ``_cancel_stalled_forward_tasks`` rewound to ``queued`` are relaunched
        as soon as the fresh client is online.
        """
        await self._reconnect_bot_client()

    async def _ensure_bot_online(self) -> None:
        """Manual path: verify the bot answers, reconnecting once if needed."""
        config = self.settings.telegram
        if not config.bot_enabled or not self.settings.telegram_bot_token:
            raise TelegramSetupError("机器人转发下载未启用")
        if self.bot_client and self._bot_ready:
            try:
                await asyncio.wait_for(
                    self.bot_client.get_me(), timeout=WATCHDOG_PROBE_TIMEOUT
                )
                if self.bot_client.is_connected():
                    self.bot_error = ""
                    return
            except Exception:
                pass
        if not await self._reconnect_bot_client():
            raise TelegramSetupError(
                f"机器人当前离线（{self.bot_error}），网络恢复后会自动重连"
            )

    async def _attempt_reconnect(self) -> None:
        """Re-establish the listener client after a network outage.

        The auth lock is held so a rebuild can never race a login or a channel
        save that is using ``self.client`` at the same moment.
        """
        await asyncio.sleep(WATCHDOG_RETRY_DELAY)
        if self.status not in {"running", "connection_error"}:
            return
        try:
            async with self._auth_lock:
                await self._safe_disconnect(self.client)
                self.client = None
                self._client_identity = None
                self._handler_registered = False
                client = await asyncio.wait_for(
                    self._ensure_client(), timeout=RECONNECT_TIMEOUT
                )
                if not await asyncio.wait_for(
                    client.is_user_authorized(), timeout=WATCHDOG_PROBE_TIMEOUT
                ):
                    self.status = "login_required"
                    self.connection_error = "会话已失效，请重新登录"
                    return
                await self._activate_listener()
            self.connection_error = ""
            logger.info("Telegram 监听已重连成功")
        except asyncio.TimeoutError:
            self.status = "connection_error"
            self.connection_error = "重连超时，网络或代理仍不可用"
            logger.warning("Telegram 监听重连超时，将在下次检查重试")
        except Exception as exc:
            self.status = "connection_error"
            self.connection_error = str(exc) or exc.__class__.__name__
            logger.warning(
                "Telegram 监听重连失败：%s，将在下次检查重试", self.connection_error
            )
