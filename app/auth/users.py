"""Аккаунты по логину и паролю.

Пароль хранится как ``scrypt$<salt hex>$<digest hex>`` (stdlib, без внешних зависимостей).
Реестр пользователей — кэш в памяти поверх Postgres, по той же схеме, что и счета:
без БД всё живёт в памяти до перезапуска.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import uuid
from dataclasses import dataclass

from app.db.repositories import player_repository

log = logging.getLogger(__name__)

LOGIN_RE = re.compile(r"^[a-z0-9_.-]{3,32}$")
MIN_PASSWORD = 6
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(actual.hex(), digest)


class UserError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class User:
    player_id: str
    login: str
    nickname: str
    password_hash: str


class UserService:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._lock = asyncio.Lock()

    async def register(self, login: str, password: str, nickname: str | None) -> User:
        login = login.strip().lower()
        if not LOGIN_RE.match(login):
            raise UserError("Логин: 3–32 символа, латиница, цифры, _ . -")
        if len(password) < MIN_PASSWORD:
            raise UserError(f"Пароль короче {MIN_PASSWORD} символов")
        nickname = (nickname or "").strip()[:24] or login
        async with self._lock:
            if login in self._users or await player_repository.get_by_login(login):
                raise UserError("Логин занят", status=409)
            user = User(f"user-{uuid.uuid4().hex[:12]}", login, nickname, hash_password(password))
            try:
                await player_repository.create_user(
                    user.player_id, login, nickname, user.password_hash
                )
            except ValueError as exc:
                raise UserError("Логин занят", status=409) from exc
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось записать пользователя %s в БД: %s", login, exc)
            self._users[login] = user
        return user

    async def authenticate(self, login: str, password: str) -> User | None:
        login = login.strip().lower()
        user = self._users.get(login)
        if user is None:
            row = await player_repository.get_by_login(login)
            if row is None or not row.get("password_hash"):
                return None
            user = User(row["id"], login, row["nickname"], row["password_hash"])
            self._users[login] = user
        return user if verify_password(password, user.password_hash) else None


users = UserService()
