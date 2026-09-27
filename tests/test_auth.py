"""Авторизация по логину и паролю: регистрация, вход, продление токена на 3 дня."""
from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import FastAPI

from app.api import http as http_api
from app.auth.tokens import AuthError, decode_token, issue_token, sessions, verify_token
from app.auth.users import hash_password, verify_password
from app.config import settings

app = FastAPI()
app.include_router(http_api.router)


def post(path: str, body: dict) -> httpx.Response:
    async def go() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.post(path, json=body)

    return asyncio.run(go())


def fresh_login() -> str:
    return f"user_{uuid.uuid4().hex[:8]}"


def test_password_hash_roundtrip():
    stored = hash_password("secret-1")
    assert stored.startswith("scrypt$")
    assert verify_password("secret-1", stored)
    assert not verify_password("secret-2", stored)
    assert not verify_password("secret-1", "garbage")


def test_register_then_login_same_account():
    login = fresh_login()
    reg = post(
        "/auth/register", {"login": login.upper(), "password": "hunter22", "nickname": "Женя"}
    )
    assert reg.status_code == 201, reg.text
    claims = decode_token(reg.json()["token"])
    assert not claims.guest
    assert claims.expires_at - claims.issued_at == settings.account_token_ttl_seconds
    assert reg.json()["nickname"] == "Женя"

    ok = post("/auth/login", {"login": login, "password": "hunter22"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["player_id"] == reg.json()["player_id"]
    assert ok.json()["balance"] == settings.guest_start_balance

    bad = post("/auth/login", {"login": login, "password": "hunter23"})
    assert bad.status_code == 401
    assert post("/auth/login", {"login": fresh_login(), "password": "hunter22"}).status_code == 401


def test_register_rejects_duplicate_and_bad_input():
    login = fresh_login()
    assert post("/auth/register", {"login": login, "password": "hunter22"}).status_code == 201
    assert post("/auth/register", {"login": login, "password": "another1"}).status_code == 409
    bad = post("/auth/register", {"login": "bad login!", "password": "hunter22"})
    assert bad.status_code == 400
    assert post("/auth/register", {"login": fresh_login(), "password": "123"}).status_code == 422


def test_refresh_extends_live_token_and_rejects_expired():
    sid = asyncio.run(sessions.issue("user-abc"))
    short, _ = issue_token("user-abc", "Женя", guest=False, ttl=60, session_id=sid)
    res = post("/auth/refresh", {"token": short})
    assert res.status_code == 200, res.text
    renewed = decode_token(res.json()["token"])
    assert renewed.player_id == "user-abc"
    assert renewed.expires_at - renewed.issued_at == settings.account_token_ttl_seconds
    assert renewed.expires_at > decode_token(short).expires_at

    sid = asyncio.run(sessions.issue("user-abc"))
    expired, _ = issue_token("user-abc", "Женя", guest=False, ttl=-10, session_id=sid)
    assert post("/auth/refresh", {"token": expired}).status_code == 401
    assert post("/auth/refresh", {"token": "not-a-jwt"}).status_code == 401


def test_new_token_revokes_previous_ones():
    login = fresh_login()
    first = post("/auth/register", {"login": login, "password": "hunter22"}).json()["token"]
    second = post("/auth/refresh", {"token": first}).json()["token"]
    assert second != first
    # продление отозвало первый токен, второй живёт
    assert post("/auth/refresh", {"token": first}).status_code == 401
    assert post("/auth/refresh", {"token": second}).status_code == 200
    # новый вход по паролю отзывает всё выданное раньше (и на других устройствах)
    third = post("/auth/login", {"login": login, "password": "hunter22"}).json()["token"]
    assert post("/auth/refresh", {"token": second}).status_code == 401
    assert asyncio.run(verify_token(third)).session_id == decode_token(third).session_id
    # токен без jti (выпущен до отзыва) и токен с чужим jti не проходят
    legacy, _ = issue_token("user-legacy", "X", guest=False)
    forged, _ = issue_token(decode_token(third).player_id, "X", guest=False, session_id="0" * 32)
    for bad in (legacy, forged):
        try:
            asyncio.run(verify_token(bad))
        except AuthError:
            pass
        else:
            raise AssertionError("отозванный токен принят")


if __name__ == "__main__":
    import sys

    from tests.helpers import run_module

    sys.exit(run_module(sys.modules[__name__]))
