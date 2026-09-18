"""Сквозной прогон: настоящий bot_client.py играет по настоящему сокету.

Поднимается маленький сервер на стандартной библиотеке, который выдаёт токен
по HTTP, принимает WebSocket и передаёт команды в НАСТОЯЩИЙ сервисный слой
(реестр столов, актор стола, движок). Затем запускается неизменённый
``scripts/bot_client.py``.

Тест закрывает то, что нельзя проверить иначе: сам бот, встроенный
WebSocket-клиент и формат сообщений — целиком, от рукопожатия до вскрытия.
Не покрывает только разбор запросов в ``app/api`` (там FastAPI).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import bot_client  # noqa: E402
from app.auth.tokens import decode_token, issue_guest_token  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.deck_provider import deck_provider  # noqa: E402
from app.protocol.messages import ServerMessageType, out  # noqa: E402
from app.services.accounts import accounts  # noqa: E402
from app.services.hub import Connection, hub  # noqa: E402
from app.services.registry import registry  # noqa: E402
from app.services.table import TableError  # noqa: E402
from ws_client import OP_CLOSE, OP_TEXT, encode_frame, read_frame  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FrameSocket:
    """Адаптер: то, что хаб считает сокетом, поверх обычного TCP-потока."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()
        self.closed = False

    async def send_json(self, message: dict) -> None:
        if self.closed:
            raise RuntimeError("сокет закрыт")
        payload = json.dumps(message, ensure_ascii=False).encode()
        async with self._lock:
            # сервер обязан слать НЕмаскированные кадры — заодно проверяем,
            # что клиент такие понимает
            self._writer.write(encode_frame(payload, OP_TEXT))
            await self._writer.drain()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        try:
            async with self._lock:
                self._writer.write(encode_frame(code.to_bytes(2, "big"), OP_CLOSE))
                await self._writer.drain()
        except Exception:  # noqa: BLE001
            pass


class MiniServer:
    """HTTP + WebSocket на голой стандартной библиотеке, поверх реального стека."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.errors: list[str] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    # ---- разбор запроса ----------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, target, _ = request_line.decode("latin-1").split(" ", 2)
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()

            if method == "POST" and target.startswith("/auth/guest"):
                await self._auth(reader, writer, headers)
            elif method == "GET" and target.startswith("/ws"):
                await self._websocket(reader, writer, headers, target)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            writer.close()

    async def _auth(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, headers: dict[str, str]
    ) -> None:
        length = int(headers.get("content-length", "0"))
        body = json.loads(await reader.readexactly(length)) if length else {}
        token, claims = issue_guest_token(body.get("nickname", "Guest"))
        account = await accounts.ensure(claims.player_id, claims.nickname)
        payload = json.dumps(
            {
                "token": token,
                "player_id": claims.player_id,
                "nickname": claims.nickname,
                "expires_at": claims.expires_at,
                "balance": account.balance,
            }
        ).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
        )
        await writer.drain()

    async def _websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: dict[str, str],
        target: str,
    ) -> None:
        token = target.partition("token=")[2].partition("&")[0]
        claims = decode_token(token)

        digest = hashlib.sha1((headers.get("sec-websocket-key", "") + GUID).encode()).digest()
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
            + base64.b64encode(digest)
            + b"\r\n\r\n"
        )
        await writer.drain()

        socket = FrameSocket(writer)
        connection = Connection(
            player_id=claims.player_id, nickname=claims.nickname, websocket=socket
        )
        await hub.register(connection)
        await socket.send_json(
            out(
                ServerMessageType.WELCOME,
                {"player": {"player_id": claims.player_id, "nickname": claims.nickname}},
            )
        )

        try:
            while True:
                fin, opcode, payload = await read_frame(reader)
                if opcode == OP_CLOSE:
                    break
                if opcode != OP_TEXT:
                    continue
                await self._dispatch(connection, json.loads(payload.decode()))
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            hub.unregister(connection)
            table = registry.find_table_of(claims.player_id)
            if table is not None:
                try:
                    await table.submit("disconnected", {"player_id": claims.player_id})
                except TableError:
                    pass

    async def _dispatch(self, connection: Connection, message: dict) -> None:
        kind, data = message.get("t"), message.get("d", {})
        try:
            if kind == "quick_join":
                await registry.quick_join(
                    connection.player_id, connection.nickname, data.get("stake"), data.get("buy_in")
                )
            elif kind in ("action", "leave_table", "sit_out", "sit_in"):
                table = registry.find_table_of(connection.player_id)
                if table is None:
                    raise TableError(
                        __import__("app.protocol.messages", fromlist=["ErrorCode"]).ErrorCode
                        .NOT_AT_TABLE,
                        "Вы не за столом",
                    )
                command = {"action": "action", "leave_table": "leave"}.get(kind, kind)
                await table.submit(
                    command, {"player_id": connection.player_id, **data}, wait=True
                )
            elif kind == "ping":
                await connection.send(out(ServerMessageType.PONG, {}))
        except TableError as exc:
            await connection.send(
                out(ServerMessageType.ERROR, {"code": exc.code.value, "message": exc.message})
            )


async def _reset() -> None:
    await registry.shutdown()
    registry._tables.clear()   # noqa: SLF001
    registry._joining.clear()  # noqa: SLF001
    hub._by_player.clear()     # noqa: SLF001
    accounts._accounts.clear() # noqa: SLF001
    if deck_provider.count == 0:
        deck_provider.load()
    await registry.start()


def test_real_bot_client_plays_over_a_real_socket():
    """Неизменённый bot_client.py должен доиграть заданное число раздач."""

    async def scenario() -> None:
        settings.action_timeout_seconds = 3.0
        settings.inter_hand_delay_seconds = 0.05
        settings.enable_postgres = False
        await _reset()

        server = MiniServer()
        await server.start()
        try:
            hands = 5
            results = await asyncio.wait_for(
                asyncio.gather(
                    *(
                        bot_client.bot(
                            i,
                            f"http://{server.host}",
                            f"ws://{server.host}",
                            "low",
                            hands,
                            quiet=True,
                        )
                        for i in range(4)
                    ),
                    return_exceptions=True,
                ),
                timeout=90,
            )
            failed = [r for r in results if isinstance(r, BaseException)]
            assert not failed, f"боты упали: {failed}"
            assert not server.errors, f"сервер ругался: {server.errors}"

            # боты доиграли и встали из-за стола: фишки вернулись на балансы
            await asyncio.sleep(0.5)
            total = sum(a.balance for a in accounts._accounts.values())  # noqa: SLF001
            seated = sum(
                seat.stack
                for table in registry.all_tables()
                for seat in table.seats
                if seat.is_occupied
            )
            expected = 4 * settings.guest_start_balance
            assert total + seated == expected, (
                f"фишки не сошлись: {total} + {seated} вместо {expected}"
            )
        finally:
            await server.stop()

    async def guarded() -> None:
        try:
            await scenario()
        finally:
            await asyncio.shield(registry.shutdown())

    asyncio.run(guarded())


def test_bot_reports_unreachable_server_clearly():
    """Если сервер не поднят, бот обязан сказать это по-человечески, а не упасть стеком."""
    try:
        bot_client.get_token("http://127.0.0.1:1", "Бот")
    except SystemExit as exc:
        text = str(exc)
        assert "Не достучался" in text and "/health" in text, text
        return
    raise AssertionError("ожидалось понятное сообщение о недоступном сервере")


if __name__ == "__main__":
    from tests.helpers import run_module

    sys.exit(run_module(sys.modules[__name__]))
