from __future__ import annotations

import importlib.util

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import __version__
from app.models import Channel, MediaFile, MediaGroup, TelegramMessage
from app.schemas import (
    DashboardStats,
    GroupSummary,
    HealthResponse,
    HistoryRebuildRequest,
    TelegramActivateRequest,
    TelegramChannelRequest,
    TelegramCodeConfirm,
    TelegramCodeRequest,
    TelegramPasswordConfirm,
    TelegramBotRequest,
    TelegramProxyUpdate,
    ImmichSettingsRequest,
)
from app.config import save_immich_runtime_config
from app.services.organizer import OrganizerError
from app.services.telegram import TelegramSetupError

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def get_db(request: Request):
    yield from request.app.state.database.session()


@router.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest():
    return FileResponse(
        "app/static/manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/service-worker.js", include_in_schema=False)
def service_worker():
    return FileResponse(
        "app/static/service-worker.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": request.app.state.settings.app.name, "version": __version__},
    )


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="setup.html",
        context={"app_name": request.app.state.settings.app.name, "version": __version__},
    )


@router.get("/rebuild", response_class=HTMLResponse)
def rebuild_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="rebuild.html",
        context={"app_name": request.app.state.settings.app.name, "version": __version__},
    )


@router.get("/immich", response_class=HTMLResponse)
def immich_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="immich.html",
        context={"app_name": request.app.state.settings.app.name, "version": __version__},
    )


@router.get("/api/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        version=__version__,
        telegram_enabled=settings.telegram.enabled,
        immich_enabled=settings.immich.enabled,
    )


@router.get("/api/status")
def service_status(request: Request) -> dict:
    settings = request.app.state.settings
    telegram = request.app.state.telegram
    return {
        "telegram": telegram.status,
        "telegram_error": getattr(telegram, "connection_error", ""),
        "bot_enabled": settings.telegram.bot_enabled,
        "bot_running": bool(
            telegram.bot_client and telegram.bot_client.is_connected()
        ),
        "bot_error": getattr(telegram, "bot_error", ""),
        "organizer": "enabled" if settings.organizer.enabled else "disabled",
        "immich": "configured" if request.app.state.immich.configured else "disabled",
        "safe_mode": not settings.telegram.enabled,
        "download_acceleration": (
            "cryptg" if importlib.util.find_spec("cryptg") else "python"
        ),
        "storage": {
            "download_path": str(settings.storage.download_path),
            "library_path": str(settings.storage.library_path),
            "rebuild_path": str(settings.storage.rebuild_path),
        },
    }


@router.get("/api/setup/state")
async def setup_state(request: Request) -> dict:
    return await request.app.state.telegram.setup_state()


@router.get("/api/settings/configured-channels")
def configured_channels(request: Request) -> dict:
    """Return saved channels without touching the Telegram connection.

    The history-rebuild picker only needs local configuration.  Reusing the
    full setup-state endpoint made that page wait behind a reconnect or an
    authorization check even though the channels were already on disk.
    """
    return {
        "channels": [
            channel.model_dump(mode="json")
            for channel in request.app.state.settings.telegram.channels
        ]
    }


@router.post("/api/setup/request-code")
async def setup_request_code(
    payload: TelegramCodeRequest, request: Request
) -> dict:
    try:
        return await request.app.state.telegram.request_login_code(
            api_id=payload.api_id,
            api_hash=payload.api_hash,
            phone=payload.phone,
            proxy_url=payload.proxy_url,
        )
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/settings/proxy")
async def update_proxy(payload: TelegramProxyUpdate, request: Request) -> dict:
    try:
        return await request.app.state.telegram.update_proxy(payload.proxy_url)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/settings/bot")
async def update_bot(payload: TelegramBotRequest, request: Request) -> dict:
    try:
        return await request.app.state.telegram.configure_bot(
            enabled=payload.enabled,
            token=payload.token,
            max_concurrent_downloads=payload.max_concurrent_downloads,
        )
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/settings/available-channels")
async def available_channels(request: Request) -> list[dict]:
    try:
        return await request.app.state.telegram.available_channels()
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/settings/channels/{chat_id}")
async def upsert_channel(
    chat_id: int,
    payload: TelegramChannelRequest,
    request: Request,
) -> dict:
    if chat_id != payload.chat_id:
        raise HTTPException(status_code=400, detail="URL 和表单中的 Chat ID 不一致")
    try:
        return await request.app.state.telegram.upsert_channel(
            channel_name=payload.channel_name,
            chat_id=payload.chat_id,
            start_message_id=payload.start_message_id,
            start_mode=payload.start_mode,
            start_date=payload.start_date,
            enabled=payload.enabled,
            grouping_mode=payload.grouping_mode,
            marker_text=payload.marker_text,
            advertisement_policy=payload.advertisement_policy,
            advertisement_keywords=payload.advertisement_keywords,
            display_spacing_hours=payload.display_spacing_hours,
            timeline_mode=payload.timeline_mode,
            display_order=payload.display_order,
            max_concurrent_downloads=payload.max_concurrent_downloads,
        )
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/settings/channels/{chat_id}")
async def delete_channel(chat_id: int, request: Request) -> dict:
    try:
        return await request.app.state.telegram.remove_channel(chat_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/settings/channels/{chat_id}/finalize")
async def finalize_channel_subject(chat_id: int, request: Request) -> dict:
    try:
        return await request.app.state.telegram.finalize_subject(chat_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/settings/channels/{chat_id}/repair-order")
async def repair_channel_order(chat_id: int, request: Request) -> dict:
    try:
        return await request.app.state.rebuild.repair_channel_order(chat_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/setup/confirm-code")
async def setup_confirm_code(
    payload: TelegramCodeConfirm, request: Request
) -> dict:
    try:
        return await request.app.state.telegram.confirm_login_code(payload.code)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/setup/confirm-password")
async def setup_confirm_password(
    payload: TelegramPasswordConfirm, request: Request
) -> dict:
    try:
        return await request.app.state.telegram.confirm_password(payload.password)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/setup/activate")
async def setup_activate(
    payload: TelegramActivateRequest, request: Request
) -> dict:
    try:
        return await request.app.state.telegram.activate(
            channel_name=payload.channel_name,
            chat_id=payload.chat_id,
            start_message_id=payload.start_message_id,
            start_mode=payload.start_mode,
            start_date=payload.start_date,
            proxy_url=payload.proxy_url,
            grouping_mode=payload.grouping_mode,
            marker_text=payload.marker_text,
            advertisement_policy=payload.advertisement_policy,
            advertisement_keywords=payload.advertisement_keywords,
            display_spacing_hours=payload.display_spacing_hours,
            timeline_mode=payload.timeline_mode,
            display_order=payload.display_order,
            max_concurrent_downloads=payload.max_concurrent_downloads,
        )
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/rebuild/tasks")
def rebuild_tasks(request: Request) -> list[dict]:
    return request.app.state.rebuild.list_tasks()


@router.get("/api/downloads/live/status")
def live_download_status(request: Request) -> dict:
    return request.app.state.telegram.live_download_status()


@router.get("/api/downloads/bot/status")
def bot_download_status(request: Request) -> dict:
    return request.app.state.telegram.forward_download_status()


@router.post("/api/downloads/bot/tasks/{task_id}/pause")
async def pause_bot_download(task_id: int, request: Request) -> dict:
    try:
        return await request.app.state.telegram.pause_forward_task(task_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/downloads/bot/tasks/{task_id}/resume")
async def resume_bot_download(task_id: int, request: Request) -> dict:
    try:
        return await request.app.state.telegram.resume_forward_task(task_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/downloads/live/pause")
async def pause_live_downloads(request: Request) -> dict:
    return await request.app.state.telegram.pause_live_downloads()


@router.post("/api/downloads/live/resume")
async def resume_live_downloads(request: Request) -> dict:
    return await request.app.state.telegram.resume_live_downloads()


@router.post("/api/rebuild/tasks")
async def create_rebuild_task(
    payload: HistoryRebuildRequest, request: Request
) -> dict:
    try:
        task = await request.app.state.rebuild.create(
            chat_id=payload.chat_id,
            channel_name=payload.channel_name,
            start_date=payload.start_date,
            end_date=payload.end_date,
            grouping_mode=payload.grouping_mode,
            marker_text=payload.marker_text,
            advertisement_policy=payload.advertisement_policy,
            display_spacing_hours=payload.display_spacing_hours,
            timeline_mode=payload.timeline_mode,
            generate_xmp=payload.generate_xmp,
            max_concurrent_downloads=payload.max_concurrent_downloads,
        )
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": task.id, "status": task.status}


@router.post("/api/rebuild/tasks/{task_id}/pause")
async def pause_rebuild_task(task_id: int, request: Request) -> dict:
    try:
        return await request.app.state.rebuild.pause(task_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/rebuild/tasks/{task_id}/resume")
async def resume_rebuild_task(task_id: int, request: Request) -> dict:
    try:
        return await request.app.state.rebuild.resume(task_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/rebuild/tasks/{task_id}/cancel")
async def cancel_rebuild_task(task_id: int, request: Request) -> dict:
    try:
        return await request.app.state.rebuild.cancel(task_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/rebuild/tasks/{task_id}/repair-order")
async def repair_rebuild_order(task_id: int, request: Request) -> dict:
    try:
        return await request.app.state.rebuild.repair_order(task_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/rebuild/tasks/{task_id}")
async def delete_rebuild_task(task_id: int, request: Request) -> dict:
    try:
        return await request.app.state.rebuild.delete(task_id)
    except TelegramSetupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/stats", response_model=DashboardStats)
def stats(db: Session = Depends(get_db)) -> DashboardStats:
    count = lambda model: db.scalar(select(func.count()).select_from(model)) or 0
    return DashboardStats(
        channels=count(Channel),
        messages=count(TelegramMessage),
        media_files=count(MediaFile),
        groups=count(MediaGroup),
        pending_groups=db.scalar(
            select(func.count()).select_from(MediaGroup).where(MediaGroup.status == "pending")
        )
        or 0,
        organized_groups=db.scalar(
            select(func.count())
            .select_from(MediaGroup)
            .where(MediaGroup.status == "organized")
        )
        or 0,
        failed_files=db.scalar(
            select(func.count()).select_from(MediaFile).where(MediaFile.state == "error")
        )
        or 0,
    )


@router.get("/api/groups", response_model=list[GroupSummary])
def groups(db: Session = Depends(get_db)) -> list[GroupSummary]:
    rows = db.execute(
        select(MediaGroup, func.count(MediaFile.id))
        .outerjoin(MediaFile)
        .group_by(MediaGroup.id)
        .order_by(MediaGroup.created_at.desc())
        .limit(100)
    ).all()
    return [
        GroupSummary.model_validate(group).model_copy(update={"file_count": file_count})
        for group, file_count in rows
    ]


@router.post("/api/groups/{group_id}/organize")
async def organize(
    group_id: int, request: Request
) -> dict:
    if not request.app.state.settings.organizer.enabled:
        raise HTTPException(status_code=409, detail="整理器未启用")
    try:
        group = await request.app.state.telegram.organize_now(group_id)
    except OrganizerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": group.id, "status": group.status, "output_path": group.output_path}


@router.get("/api/immich/status")
async def immich_status(request: Request) -> dict:
    try:
        return await request.app.state.immich.server_info()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Immich 连接失败：{exc}") from exc


@router.get("/api/immich/settings")
async def immich_settings(request: Request) -> dict:
    return await request.app.state.immich.settings_state()


@router.put("/api/immich/settings")
async def update_immich_settings(
    payload: ImmichSettingsRequest, request: Request
) -> dict:
    if payload.enabled and not (
        payload.api_key.strip() or request.app.state.settings.immich_api_key
    ):
        raise HTTPException(status_code=400, detail="首次连接请填写 Immich API Key")
    if (
        payload.library_id
        and payload.forwarded_library_id
        and payload.library_id == payload.forwarded_library_id
    ):
        raise HTTPException(
            status_code=400,
            detail="主人物库和机器人下载库必须选择两个不同的 Immich 外部库",
        )
    save_immich_runtime_config(**payload.model_dump())
    settings = request.app.state.settings
    settings.immich = settings.immich.model_validate(payload.model_dump(exclude={"api_key"}))
    request.app.state.immich.settings = settings
    request.app.state.immich_sync.settings = settings
    return {"message": "Immich 设置已保存", **await request.app.state.immich.settings_state()}


@router.get("/api/immich/libraries")
async def immich_libraries(request: Request) -> list[dict]:
    try:
        return await request.app.state.immich.libraries()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"读取 Immich 外部库失败：{exc}") from exc


@router.get("/api/immich/refresh-status")
def immich_refresh_status(request: Request) -> dict:
    return request.app.state.immich_sync.status()


@router.get("/api/immich/album-report")
def immich_album_report(request: Request) -> dict:
    """Per-album counts from the last sync, kept across restarts."""
    return request.app.state.immich_sync.album_report()


@router.post("/api/immich/scan")
async def immich_scan(request: Request) -> dict:
    try:
        request.app.state.immich_sync.request(
            "main", refresh_metadata=True, force=True
        )
        return {
            "message": (
                "已启动序影自动校正：扫描整理库、等待 Immich 入库，"
                "随后自动同步人物相册"
            )
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"触发 Immich 扫描失败：{exc}") from exc


@router.post("/api/immich/sync/{target}")
async def immich_sync_pipeline(target: str, request: Request) -> dict:
    if target not in {"main", "forwarded"}:
        raise HTTPException(status_code=404, detail="未知的 Immich 同步目标")
    queued = request.app.state.immich_sync.request(
        target, refresh_metadata=(target == "main"), force=True
    )
    if not queued:
        raise HTTPException(status_code=409, detail="请先完成 Immich 连接设置")
    return {
        "message": (
            "频道人物库同步已经排队"
            if target == "main"
            else "机器人下载库同步已经排队"
        ),
        "target": target,
        "queued": True,
    }


@router.post("/api/immich/forwarded/scan")
async def immich_forwarded_scan(request: Request) -> dict:
    try:
        request.app.state.immich_sync.request("forwarded", force=True)
        return {"message": "已启动机器人下载库扫描与相册同步"}
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"触发机器人媒体库扫描失败：{exc}"
        ) from exc


@router.post("/api/immich/forwarded/albums/sync")
async def immich_forwarded_album_sync(request: Request) -> dict:
    try:
        return await request.app.state.immich_sync.sync_now("forwarded")
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"同步机器人 Immich 相册失败：{exc}"
        ) from exc


@router.post("/api/immich/albums/sync")
async def immich_album_sync(request: Request) -> dict:
    try:
        return await request.app.state.immich_sync.sync_now("main")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"同步 Immich 相册失败：{exc}") from exc
