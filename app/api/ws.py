"""WebSocket-эндпоинт: единственный канал общения Unity-клиента с сервером.

Подключение::

    ws://host:8000/ws?token=<JWT>

Токен можно передать и первым сообщением ``{"t": "auth", "d": {"token": "..."}}``.
Пока соединение не авторизовано, любые другие сообщения отклоняются.

Реконнект: если у игрока уже есть место за столом, при подключении он получает
``table_snapshot`` со своими карманными картами, текущей улицей, банком и
остатком времени на ход — клиенту достаточно просто отрисовать снимок.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.auth.tokens import AuthError, TokenClaims, decode_token
from app.config import settings
from app.protocol.messages import (
    ActionPayload,
    AuthPayload,
    ChatPayload,
    ClientMessageType,
    Envelope,
    ErrorCode,
    QuickJoinPayload,
    RebuyPayload,
    ServerMessageType,
    error,
    out,
)
from app.services.accounts import accounts
from app.services.hub import Connection, hub
from app.services.registry import registry
from app.services.table import TableError

log = logging.getLogger(__name__)
router = APIRouter()

#: не больше стольких сообщений в секунду от одного клиента
RATE_LIMIT_PER_SECOND = 20


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str | None = None) -> None:
    await websocket.accept()

    claims = await _authenticate(websocket, token)
    if claims is None:
        return

    account = await accounts.ensure(claims.player_id, claims.nickname)
    connection = Connection(
        player_id=claims.player_id, nickname=claims.nickname, websocket=websocket
    )
    await hub.register(connection)

    await connection.send(
        out(
            ServerMessageType.WELCOME,
            {
                "player": {
                    "player_id": claims.player_id,
                    "nickname": claims.nickname,
                    "guest": claims.guest,
                    "balance": account.balance,
                },
                "stakes": [s.as_dict for s in settings.stakes],
                "max_seats": settings.max_seats,
                "action_timeout": settings.action_timeout_seconds,
                "server_time": time.time(),
            },
        )
    )

    # реконнект: игрок уже сидит за столом — возвращаем на место
    table = registry.find_table_of(claims.player_id)
    if table is not None:
        connection.table_id = table.table_id
        try:
            await table.submit("connected", {"player_id": claims.player_id})
        except TableError as exc:
            # стол мог умереть между поиском и командой — не рвём сокет из-за этого
            log.warning("Реконнект к столу %s не удался: %s", table.table_id, exc.message)
            await connection.send(error(exc.code, exc.message))

    window_start = time.monotonic()
    message_count = 0

    try:
        while True:
            raw: dict | None = None
            malformed: str | None = None
            try:
                raw = await websocket.receive_json()
            except (json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError):
                # битый или бинарный фрейм — это ошибка клиента, а не повод рвать
                # соединение; но такие фреймы тоже считаются рейт-лимитом,
                # иначе клиент может бесконечно гонять «мусор → ошибка в ответ»
                malformed = "Ожидается текстовый фрейм с JSON-объектом"

            now = time.monotonic()
            if now - window_start >= 1.0:
                window_start, message_count = now, 0
            message_count += 1
            if message_count > RATE_LIMIT_PER_SECOND:
                await connection.send(
                    error(ErrorCode.RATE_LIMITED, "Слишком много сообщений, притормозите")
                )
                continue

            if malformed is not None:
                await connection.send(error(ErrorCode.BAD_MESSAGE, malformed))
                continue

            await _dispatch(connection, raw or {})
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("Ошибка в сокете игрока %s", claims.player_id)
    finally:
        connection.closed = True
        hub.unregister(connection)
        # если игрок уже переподключился с другого устройства, его текущее
        # соединение живо — стол не должен считать игрока отключившимся
        superseded = hub.get(claims.player_id) is not None
        current = registry.find_table_of(claims.player_id)
        if current is not None and not superseded:
            try:
                await current.submit("disconnected", {"player_id": claims.player_id})
            except TableError:
                # исключение из finally затёрло бы нормальное закрытие сокета
                pass


async def _authenticate(websocket: WebSocket, token: str | None) -> TokenClaims | None:
    """Достаёт claims из query-параметра или ждёт первое сообщение ``auth``."""
    if token:
        try:
            return decode_token(token)
        except AuthError as exc:
            await websocket.send_json(error(ErrorCode.UNAUTHORIZED, str(exc)))
            await websocket.close(code=4401)
            return None
    try:
        raw = await websocket.receive_json()
        envelope = Envelope.model_validate(raw)
        if envelope.t is not ClientMessageType.AUTH:
            raise AuthError("Первым сообщением ожидается auth")
        payload = AuthPayload.model_validate(envelope.d)
        return decode_token(payload.token)
    except (AuthError, ValidationError, KeyError, json.JSONDecodeError) as exc:
        await websocket.send_json(error(ErrorCode.UNAUTHORIZED, str(exc)))
        await websocket.close(code=4401)
        return None
    except WebSocketDisconnect:
        return None


async def _dispatch(connection: Connection, raw: dict) -> None:
    try:
        envelope = Envelope.model_validate(raw)
    except ValidationError as exc:
        await connection.send(
            error(ErrorCode.BAD_MESSAGE, f"Не разобрал сообщение: {exc.errors()[0]['msg']}")
        )
        return

    rid = envelope.rid
    kind = envelope.t

    try:
        if kind is ClientMessageType.PING:
            await connection.send(out(ServerMessageType.PONG, {"server_time": time.time()}, rid))

        elif kind is ClientMessageType.AUTH:
            await connection.send(out(ServerMessageType.WELCOME, {"already_authorized": True}, rid))

        elif kind is ClientMessageType.LIST_STAKES:
            await connection.send(
                out(
                    ServerMessageType.STAKES,
                    {
                        "stakes": [s.as_dict for s in settings.stakes],
                        "tables": registry.stats,
                    },
                    rid,
                )
            )

        elif kind is ClientMessageType.QUICK_JOIN:
            payload = QuickJoinPayload.model_validate(envelope.d)
            table, result = await registry.quick_join(
                connection.player_id, connection.nickname, payload.stake, payload.buy_in
            )
            connection.table_id = table.table_id
            log.info(
                "Автоподбор: %s -> стол %s, место %s",
                connection.nickname, table.table_id, result.get("seat_no"),
            )

        elif kind is ClientMessageType.REQUEST_SNAPSHOT:
            table = _require_table(connection)
            await table.submit("snapshot", {"player_id": connection.player_id})

        elif kind is ClientMessageType.ACTION:
            payload = ActionPayload.model_validate(envelope.d)
            table = _require_table(connection)
            await table.submit(
                "action",
                {
                    "player_id": connection.player_id,
                    "hand_id": payload.hand_id,
                    "action": payload.action,
                    "amount": payload.amount,
                    "rid": rid,
                },
                wait=True,
            )

        elif kind is ClientMessageType.LEAVE_TABLE:
            table = _require_table(connection)
            await table.submit("leave", {"player_id": connection.player_id}, wait=True)

        elif kind is ClientMessageType.SIT_OUT:
            table = _require_table(connection)
            await table.submit("sit_out", {"player_id": connection.player_id}, wait=True)

        elif kind is ClientMessageType.SIT_IN:
            table = _require_table(connection)
            await table.submit("sit_in", {"player_id": connection.player_id}, wait=True)

        elif kind is ClientMessageType.REBUY:
            payload = RebuyPayload.model_validate(envelope.d)
            table = _require_table(connection)
            await table.submit(
                "rebuy", {"player_id": connection.player_id, "amount": payload.amount}, wait=True
            )

        elif kind is ClientMessageType.CHAT:
            payload = ChatPayload.model_validate(envelope.d)
            table = _require_table(connection)
            await table.submit(
                "chat", {"player_id": connection.player_id, "text": payload.text}, wait=True
            )

        else:  # pragma: no cover
            await connection.send(error(ErrorCode.BAD_MESSAGE, "Необработанный тип", rid))

    except TableError as exc:
        await connection.send(error(exc.code, exc.message, rid))
    except ValidationError as exc:
        await connection.send(
            error(ErrorCode.BAD_MESSAGE, f"Некорректные поля: {exc.errors()[0]['msg']}", rid)
        )
    except Exception:  # noqa: BLE001
        log.exception("Ошибка обработки %s от %s", kind, connection.player_id)
        await connection.send(error(ErrorCode.INTERNAL, "Внутренняя ошибка сервера", rid))


def _require_table(connection: Connection):
    table = registry.find_table_of(connection.player_id)
    if table is None:
        raise TableError(ErrorCode.NOT_AT_TABLE, "Вы не за столом — сначала quick_join")
    connection.table_id = table.table_id
    return table
