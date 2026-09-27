"""JWT: гостевой (30 дней) и аккаунта (3 дня, продлевается через /auth/refresh).

Отзыв: в токене лежит ``jti``, сервер помнит текущий ``jti`` игрока (``sessions``).
Каждая выдача меняет его, поэтому после продления или нового входа старый токен
перестаёт приниматься, хотя подпись и срок у него в порядке.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from dataclasses import dataclass

import jwt

from app.config import settings
from app.db.repositories import player_repository

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TokenClaims:
    player_id: str
    nickname: str
    guest: bool
    issued_at: int
    expires_at: int
    #: ``jti``; None у токенов, выпущенных до появления отзыва — такие не принимаются
    session_id: str | None = None


class AuthError(Exception):
    pass


def issue_token(
    player_id: str,
    nickname: str,
    *,
    guest: bool,
    ttl: int | None = None,
    session_id: str | None = None,
) -> tuple[str, TokenClaims]:
    if ttl is None:
        ttl = settings.guest_token_ttl_seconds if guest else settings.account_token_ttl_seconds
    now = int(time.time())
    exp = now + ttl
    payload = {"sub": player_id, "nick": nickname, "guest": guest, "iat": now, "exp": exp}
    if session_id:
        payload["jti"] = session_id
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    claims = TokenClaims(
        player_id=player_id,
        nickname=nickname,
        guest=guest,
        issued_at=now,
        expires_at=exp,
        session_id=session_id,
    )
    return token, claims


def issue_guest_token(nickname: str, player_id: str | None = None) -> tuple[str, TokenClaims]:
    nickname = (nickname or "").strip()[:24] or "Guest"
    return issue_token(player_id or f"guest-{uuid.uuid4().hex[:12]}", nickname, guest=True)


def decode_token(token: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Токен истёк") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Некорректный токен") from exc
    return TokenClaims(
        player_id=str(payload["sub"]),
        nickname=str(payload.get("nick", "Guest")),
        guest=bool(payload.get("guest", True)),
        issued_at=int(payload.get("iat", 0)),
        expires_at=int(payload.get("exp", 0)),
        session_id=payload.get("jti"),
    )


class SessionStore:
    """Текущий ``jti`` каждого игрока: память поверх Postgres, как счета и логины."""

    def __init__(self) -> None:
        self._current: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def issue(self, player_id: str) -> str:
        """Новый ``jti``; все ранее выданные токены игрока с этого момента отозваны."""
        session_id = uuid.uuid4().hex
        async with self._lock:
            self._current[player_id] = session_id
        try:
            await player_repository.set_session(player_id, session_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось записать сессию игрока %s: %s", player_id, exc)
        return session_id

    async def is_current(self, player_id: str, session_id: str) -> bool:
        async with self._lock:
            current = self._current.get(player_id)
            if current is None:
                try:
                    current = await player_repository.get_session(player_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Не удалось прочитать сессию игрока %s: %s", player_id, exc)
                    current = None
                # ponytail: без БД после перезапуска сервер ничего не помнит — принимаем первый
                # предъявленный токен и дальше держимся его; с Postgres сюда не попадаем.
                current = current or session_id
                self._current[player_id] = current
        return hmac.compare_digest(current, session_id)


sessions = SessionStore()


async def verify_token(token: str) -> TokenClaims:
    """``decode_token`` + проверка, что токен не отозван более новым."""
    claims = decode_token(token)
    if not claims.session_id or not await sessions.is_current(
        claims.player_id, claims.session_id
    ):
        raise AuthError("Токен отозван: был выполнен новый вход")
    return claims
