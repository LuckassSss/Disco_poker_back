# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Poker Server (FastAPI)

Сервер покерной игры (No-Limit Texas Hold'em, 5 мест). Python 3.11+, FastAPI, WebSocket.
Клиент (Unity) — отдельный репозиторий, локально лежит рядом: `../client/` (свой `CLAUDE.md`).
Клиент тонкий: правила, банк и карты живут только здесь.

## Структура
- `app/core/` — синхронное ядро без сети и asyncio (`game.py` движок раздачи, `pot.py`, `hand_eval.py`, `deck_provider.py`).
- `app/services/` — `registry.py` (реестр столов, `quick_join`), `accounts.py` (баланс вне стола: бай-ин списывает,
  выход возвращает; кэш в памяти, итог в `app/db/`), `table.py` (стол-актор, по asyncio-задаче на стол),
  `hub.py` (player_id → живой сокет).
- `app/api/ws.py` — эндпоинт `/ws`; `app/api/http.py` — `/auth/register|login|refresh|guest`, `/health`.
- `app/auth/` — `tokens.py` (JWT, `SessionStore` отзыва), `users.py` (логины, scrypt).
- `app/protocol/messages.py` — схема сообщений. Без Postgres/Redis работает в памяти.
- `Dockerfile`, `docker-compose.yml` (postgres + redis + api, `make up`).

**В рантайме сервера нет генерации случайных чисел**: колоды заранее замешаны (`scripts/generate_decks.py` →
`data/decks.json`), `deck_provider.py` выдаёт их по глобальному счётчику (`INCR` в Redis, без него — локальный).
`random`/`secrets` для карт в ядро не добавлять.

**Справочник протокола: `docs/PROTOCOL.md`, требования к клиенту: `docs/CLIENT_NETWORKING.md`,
устройство сервера: `docs/ARCHITECTURE.md`, ТЗ: `docs/SPECIFICATION.md`.** Источник правды — markdown;
после правки пересобрать `docs/pdf/` командой `make docs-pdf`. Изменение протокола — правка и в `../client/`.

## Авторизация
`POST /auth/register|login {"login","password"[,"nickname"]}` → `token`, `player_id`, `balance`; токен аккаунта 3 дня,
`/auth/refresh` выдаёт новый. Каждая выдача отзывает прежние токены игрока: `jti` в токене, текущий — в `SessionStore`
(`tokens.py`, память поверх `players.session_id`); проверка — `verify_token`, не `decode_token`.
`POST /auth/guest {"nickname"}` (30 суток) — для ботов и `POKER_AUTOJOIN` клиента.
Пароли — `hashlib.scrypt` в `app/auth/users.py`; логины — колонки `players.login/password_hash`
(`create_all` не добавит их в уже существующую таблицу — пересоздать).

## Проверка
В `.venv`, который создаёт `scripts/run_local.sh`: `make test` (pytest; `pyproject.toml`: `asyncio_mode=auto`,
`pythonpath=.`; без pytest падает на `python tests/run_all.py`), один тест — `pytest tests/test_pot.py -k имя_теста`;
`make lint` (ruff, line-length 100); `make run` (uvicorn с reload), `make bots BOTS=4 HANDS=20`
(не 5 — иначе клиенту не останется места); `make decks COUNT=10` — перегенерировать `data/decks.json`.
Конфиг — переменные окружения (`app/config.py`), `run_local.sh` копирует `.env.example` в `.env` (не в git).
Для проверок клиента запускать с `GUEST_START_BALANCE=1000000`: по умолчанию 10 000 = бай-ину, докупиться не на что.
`curl localhost:8000/health` — жив ли; `http://localhost:8000/docs` — Swagger.

## Прогресс
Где остановились и бэклог сервера — в `PROGRESS.md`. Читать первым в новой сессии, обновлять в конце (вместе с коммитом).
