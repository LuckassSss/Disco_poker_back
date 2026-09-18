"""Асинхронное подключение к PostgreSQL."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from app.config import settings

try:  # без SQLAlchemy игровая часть должна работать: БД тут не обязательна
    from sqlalchemy.ext.asyncio import (  # type: ignore
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    SQLALCHEMY_AVAILABLE = True
except ImportError:  # pragma: no cover
    AsyncEngine = AsyncSession = async_sessionmaker = create_async_engine = None  # type: ignore
    SQLALCHEMY_AVAILABLE = False

log = logging.getLogger(__name__)

_engine: "AsyncEngine | None" = None
_session_factory: "async_sessionmaker[AsyncSession] | None" = None


async def init_db() -> bool:
    """Поднимает движок и создаёт таблицы. Возвращает False, если БД выключена/недоступна."""
    global _engine, _session_factory
    if not settings.enable_postgres:
        log.warning("Postgres отключён настройкой enable_postgres=false")
        return False
    if not SQLALCHEMY_AVAILABLE:  # pragma: no cover
        log.warning("SQLAlchemy не установлен — работаем без персистентности")
        return False
    try:
        from app.db.models import Base  # noqa: PLC0415

        engine = create_async_engine(settings.postgres_dsn, pool_pre_ping=True, future=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        _engine = engine
        _session_factory = async_sessionmaker(engine, expire_on_commit=False)
        log.info("PostgreSQL подключён")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("PostgreSQL недоступен (%s). Игра работает без персистентности.", exc)
        _engine = None
        _session_factory = None
        return False


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def is_enabled() -> bool:
    return _session_factory is not None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession | None]:
    """Контекст сессии. Отдаёт None, если БД недоступна — вызывающий код это переживает."""
    if _session_factory is None:
        yield None
        return
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
