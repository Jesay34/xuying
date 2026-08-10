from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.config import load_runtime_secrets, load_settings
from app.database import Database
from app.services.immich import ImmichClient
from app.services.immich_sync import ImmichSyncOrchestrator
from app.services.rebuild import HistoryRebuildService
from app.services.telegram import TelegramService

load_runtime_secrets()
settings = load_settings()
settings.ensure_directories()
logging.basicConfig(
    level=getattr(logging, settings.app.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
database = Database(settings.database_url)
database.create_all()
immich = ImmichClient(settings)
immich_sync = ImmichSyncOrchestrator(settings, immich)
telegram = TelegramService(settings, database.session_factory)
telegram.immich = immich_sync
rebuild = HistoryRebuildService(settings, database.session_factory, telegram, immich_sync)
telegram.channel_reconcile_callback = rebuild.schedule_channel_reconcile


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = settings
    app.state.database = database
    app.state.telegram = telegram
    app.state.immich = immich
    app.state.immich_sync = immich_sync
    app.state.rebuild = rebuild
    migration = rebuild.migrate_raw_channel_aliases()
    if migration["moved"] or migration["reused"] or migration["conflicts"]:
        logging.getLogger(__name__).info("Raw channel alias migration: %s", migration)
    await telegram.start()
    await rebuild.start()
    await immich_sync.start()
    yield
    await rebuild.stop()
    await telegram.stop()
    await immich_sync.stop()


app = FastAPI(
    title="序影 Xuying",
    description="Telegram → 媒体整理 → Immich 一体化媒体管家",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.app.host,
        port=settings.app.port,
        reload=False,
    )
