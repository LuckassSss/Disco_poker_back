"""HTTP-эндпоинты: авторизация, здоровье, лобби, аудит колод.

Сама игра идёт по WebSocket — HTTP нужен только чтобы получить токен и
посмотреть, что происходит на сервере.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.auth.tokens import AuthError, issue_token, sessions, verify_token
from app.auth.users import UserError, users
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


class CredentialsRequest(BaseModel):
    login: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class RegisterRequest(CredentialsRequest):
    nickname: str | None = Field(default=None, max_length=24)


class RefreshRequest(BaseModel):
    token: str


class AuthResponse(BaseModel):
    token: str
    player_id: str
    nickname: str
    expires_at: int
    balance: int


async def _issue(player_id: str, nickname: str, guest: bool) -> AuthResponse:
    """Выдаёт токен и отзывает все прежние токены игрока (новый ``jti``)."""
    account = await accounts.ensure(player_id, nickname)
    session_id = await sessions.issue(player_id)
    token, claims = issue_token(player_id, nickname, guest=guest, session_id=session_id)
    return AuthResponse(
        token=token,
        player_id=claims.player_id,
        nickname=claims.nickname,
        expires_at=claims.expires_at,
        balance=account.balance,
    )


@router.post("/auth/register", response_model=AuthResponse, status_code=201, tags=["auth"])
async def auth_register(body: RegisterRequest) -> AuthResponse:
    """Регистрация по логину и паролю. Сразу выдаёт токен на 3 дня."""
    try:
        user = await users.register(body.login, body.password, body.nickname)
    except UserError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return await _issue(user.player_id, user.nickname, guest=False)


@router.post("/auth/login", response_model=AuthResponse, tags=["auth"])
async def auth_login(body: CredentialsRequest) -> AuthResponse:
    user = await users.authenticate(body.login, body.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    return await _issue(user.player_id, user.nickname, guest=False)


@router.post("/auth/refresh", response_model=AuthResponse, tags=["auth"])
async def auth_refresh(body: RefreshRequest) -> AuthResponse:
    """Продление ещё живого токена: новый токен с новым сроком (3 дня для аккаунта).

    Старый токен после этого отозван. Просроченный или отозванный → 401: клиент
    показывает форму входа по логину и паролю.
    """
    try:
        claims = await verify_token(body.token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return await _issue(claims.player_id, claims.nickname, claims.guest)


@router.post("/auth/guest", response_model=AuthResponse, tags=["auth"])
async def auth_guest(body: GuestAuthRequest) -> AuthResponse:
    """Гостевой вход. Заглушка до появления настоящих аккаунтов.

    Без ``token`` создаётся новая личность. С валидным ``token`` продлевается
    существующая — так клиент переживает перезапуск приложения, не теряя баланс.
    """
    previous_id: str | None = None
    if body.token:
        try:
            previous_id = (await verify_token(body.token)).player_id
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    player_id = previous_id or f"guest-{uuid.uuid4().hex[:12]}"
    return await _issue(player_id, body.nickname.strip() or "Guest", guest=True)


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
