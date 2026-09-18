"""HTTP-эндпоинты: авторизация, здоровье, лобби, аудит колод.

Сама игра идёт по WebSocket — HTTP нужен только чтобы получить токен и
посмотреть, что происходит на сервере.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.auth.tokens import AuthError, decode_token, issue_guest_token
from app.config import settings
from app.core.deck_provider import deck_provider
from app.db.session import is_enabled as db_enabled
from app.infra.redis_client import get_redis
from app.services.accounts import accounts
from app.services.hub import hub
from app.services.registry import registry

router = APIRouter()


class GuestAuthRequest(BaseModel):
    nickname: str = Field(min_length=1, max_length=24)
    #: чтобы продлить ту же личность, клиент присылает СВОЙ прошлый токен.
    #: Просто `player_id` брать нельзя: он публичен (виден соперникам за столом),
    #: и по нему кто угодно выпустил бы себе токен на чужой аккаунт.
    token: str | None = None


class GuestAuthResponse(BaseModel):
    token: str
    player_id: str
    nickname: str
    expires_at: int
    balance: int


@router.post("/auth/guest", response_model=GuestAuthResponse, tags=["auth"])
async def auth_guest(body: GuestAuthRequest) -> GuestAuthResponse:
    """Гостевой вход. Заглушка до появления настоящих аккаунтов.

    Без ``token`` создаётся новая личность. С валидным ``token`` продлевается
    существующая — так клиент переживает перезапуск приложения, не теряя баланс.
    """
    previous_id: str | None = None
    if body.token:
        try:
            previous_id = decode_token(body.token).player_id
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    token, claims = issue_guest_token(body.nickname, previous_id)
    account = await accounts.ensure(claims.player_id, claims.nickname)
    return GuestAuthResponse(
        token=token,
        player_id=claims.player_id,
        nickname=claims.nickname,
        expires_at=claims.expires_at,
        balance=account.balance,
    )


@router.get("/health", tags=["service"])
async def health() -> dict:
    redis = get_redis()
    redis_ok = False
    if redis is not None:
        try:
            redis_ok = bool(await redis.ping())
        except Exception:  # noqa: BLE001
            redis_ok = False
    return {
        "status": "ok",
        "environment": settings.environment,
        "redis": redis_ok,
        "postgres": db_enabled(),
        "decks_loaded": deck_provider.count,
        "online": hub.online_count,
        **registry.stats,
    }


@router.get("/lobby", tags=["lobby"])
async def lobby(stake: str | None = None) -> dict:
    if stake is not None and settings.stake(stake) is None:
        raise HTTPException(status_code=404, detail="Неизвестный уровень ставок")
    return {
        "stakes": [s.as_dict for s in settings.stakes],
        "tables": registry.lobby(stake),
    }


@router.get("/decks", tags=["fairness"])
async def decks() -> dict:
    """Список заготовленных колод и текущее положение глобального счётчика.

    Отдаём только контрольные суммы, а не сами карты: иначе, зная позицию
    счётчика, клиент мог бы вычислить, какие карты придут следующими.
    """
    redis = get_redis()
    cursor = None
    if redis is not None:
        try:
            raw = await redis.get(settings.deck_cursor_key)
            cursor = int(raw) if raw is not None else 0
        except Exception:  # noqa: BLE001
            cursor = None
    return {
        "count": deck_provider.count,
        "cursor": cursor,
        "source": str(settings.decks_file.name),
        "decks": [{"id": d.deck_id, "sha256": d.sha256} for d in deck_provider.decks],
    }
