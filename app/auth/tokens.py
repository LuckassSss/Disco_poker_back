"""Авторизация. Сейчас это заглушка: гостевой токен по нику.

Контракт намеренно узкий — ``issue_guest_token`` / ``decode_token`` / ``TokenClaims``.
Когда появится настоящая регистрация, меняется только реализация выдачи, а весь
WebSocket-слой остаётся нетронутым.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt

from app.config import settings


@dataclass(frozen=True, slots=True)
class TokenClaims:
    player_id: str
    nickname: str
    guest: bool
    issued_at: int
    expires_at: int


class AuthError(Exception):
    pass


def issue_guest_token(nickname: str, player_id: str | None = None) -> tuple[str, TokenClaims]:
    nickname = (nickname or "").strip()[:24] or "Guest"
    pid = player_id or f"guest-{uuid.uuid4().hex[:12]}"
    now = int(time.time())
    exp = now + settings.guest_token_ttl_seconds
    payload = {
        "sub": pid,
        "nick": nickname,
        "guest": True,
        "iat": now,
        "exp": exp,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    claims = TokenClaims(
        player_id=pid, nickname=nickname, guest=True, issued_at=now, expires_at=exp
    )
    return token, claims


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
    )
