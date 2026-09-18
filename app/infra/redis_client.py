"""Подключение к Redis.

Redis используется для двух вещей:
  1. глобальный атомарный счётчик раздач → выбор очередной колоды;
  2. регистр стол↔инстанс и pub/sub, когда серверов станет больше одного.
"""
from __future__ import annotations

import logging

from typing import Any

try:  # пакет redis не нужен для игрового ядра — тесты ядра идут без него
    from redis.asyncio import Redis, from_url  # type: ignore
except ImportError:  # pragma: no cover
    Redis = Any  # type: ignore[misc,assignment]
    from_url = None  # type: ignore[assignment]

from app.config import settings

log = logging.getLogger(__name__)

_client: "Redis | None" = None


async def init_redis() -> "Redis | None":
    global _client
    if from_url is None:  # pragma: no cover
        log.warning("Пакет redis не установлен — работаем на локальном счётчике колод")
        return None
    try:
        client = from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        _client = client
        log.info("Redis подключён: %s", settings.redis_url)
    except Exception as exc:  # noqa: BLE001
        if not settings.allow_redis_fallback:
            raise
        log.warning("Redis недоступен (%s). Работаем на локальном счётчике колод.", exc)
        _client = None
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def get_redis() -> "Redis | None":
    return _client
