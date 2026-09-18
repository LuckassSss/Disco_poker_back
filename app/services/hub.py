"""Реестр живых WebSocket-соединений.

Один игрок — одно активное соединение. Если пришёл второй коннект с тем же
``player_id`` (открыли игру на втором устройстве), старое соединение вытесняется,
а стол об этом даже не узнаёт: для него игрок просто остался на месте.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # только для типов: сервисный слой не зависит от веб-фреймворка
    from fastapi import WebSocket

log = logging.getLogger(__name__)


class SocketLike(Protocol):
    """Всё, что нужно хабу от сокета.

    Благодаря такому узкому контракту весь сервисный слой (хаб, столы, реестр)
    не зависит от FastAPI: в интеграционных тестах сюда подставляется обычный
    объект с двумя методами, и стол этого не замечает.
    """

    async def send_json(self, message: Any) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


@dataclass
class Connection:
    player_id: str
    nickname: str
    websocket: "WebSocket | SocketLike"
    table_id: str | None = None
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    closed: bool = False

    async def send(self, message: dict) -> bool:
        if self.closed:
            return False
        try:
            async with self._send_lock:
                await self.websocket.send_json(message)
            return True
        except Exception:  # noqa: BLE001
            self.closed = True
            return False

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001
            pass


class ConnectionHub:
    def __init__(self) -> None:
        self._by_player: dict[str, Connection] = {}

    async def register(self, connection: Connection) -> Connection | None:
        """Регистрирует соединение, возвращает вытесненное старое (если было)."""
        previous = self._by_player.get(connection.player_id)
        self._by_player[connection.player_id] = connection
        if previous is not None and previous is not connection:
            await previous.close(code=4001, reason="Подключение с другого устройства")
            return previous
        return None

    def unregister(self, connection: Connection) -> None:
        current = self._by_player.get(connection.player_id)
        if current is connection:
            self._by_player.pop(connection.player_id, None)

    def get(self, player_id: str) -> Connection | None:
        return self._by_player.get(player_id)

    async def send(self, player_id: str, message: dict) -> bool:
        connection = self._by_player.get(player_id)
        if connection is None:
            return False
        return await connection.send(message)

    async def send_many(self, player_ids: list[str], message: dict) -> None:
        await asyncio.gather(
            *(self.send(pid, message) for pid in player_ids), return_exceptions=True
        )

    @property
    def online_count(self) -> int:
        return len(self._by_player)


hub = ConnectionHub()
