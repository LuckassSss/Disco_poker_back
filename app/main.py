"""Точка входа: FastAPI + WebSocket-сервер покера."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import http as http_api
from app.api import ws as ws_api
from app.config import settings
from app.core.deck_provider import deck_provider
from app.db.session import close_db, init_db
from app.infra.redis_client import close_redis, init_redis
from app.services.registry import registry

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("poker")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Запуск %s (%s)", settings.app_name, settings.environment)
    deck_provider.load()
    await init_redis()
    await init_db()
    await registry.start()
    try:
        yield
    finally:
        log.info("Останавливаем сервер…")
        await registry.shutdown()
        await close_redis()
        await close_db()


app = FastAPI(
    title="Neon Poker Backend",
    version="0.1.0",
    description=(
        "Бэкенд онлайн-покера (No-Limit Texas Hold'em, 6-max). "
        "Клиент — тонкий, вся логика и все карты живут на сервере."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(http_api.router)
app.include_router(ws_api.router)


@app.get("/", tags=["service"])
async def root() -> dict:
    return {
        "service": settings.app_name,
        "websocket": "/ws?token=<JWT>",
        "docs": "/docs",
    }


def run() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "dev",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    run()
