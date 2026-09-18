"""Минимальный WebSocket-клиент на голой стандартной библиотеке (RFC 6455).

Нужен ровно для одного: чтобы отладочные утилиты вроде ``bot_client.py``
запускались обычным ``python3`` без установки пакетов. Ставить зависимости
только ради того, чтобы посмотреть на игру, — лишний барьер.

Это НЕ замена библиотеке ``websockets`` для продакшена: тут нет сжатия
(permessage-deflate), нет ограничений на размер сообщения и нет тонкой
настройки таймаутов. Для клиента-бота этого достаточно.

Использование::

    ws = await connect("ws://localhost:8000/ws?token=…")
    await ws.send('{"t":"ping","d":{}}')
    async for message in ws:
        print(message)
    await ws.close()
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl as ssl_module
import struct
from urllib.parse import urlsplit

#: магическая строка из RFC 6455 §1.3 для проверки рукопожатия
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(Exception):
    pass


class ConnectionClosed(WebSocketError):
    def __init__(self, code: int = 1000, reason: str = "") -> None:
        super().__init__(f"соединение закрыто ({code}) {reason}".strip())
        self.code = code
        self.reason = reason


def accept_key(client_key: str) -> str:
    """Ответный ключ сервера — по нему проверяем, что это действительно WebSocket."""
    digest = hashlib.sha1((client_key + GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def encode_frame(payload: bytes, opcode: int = OP_TEXT, *, mask: bytes | None = None) -> bytes:
    """Собирает кадр. Клиент обязан маскировать данные, сервер — нет."""
    header = bytearray()
    header.append(0x80 | opcode)  # FIN=1, без расширений
    length = len(payload)
    masked_bit = 0x80 if mask is not None else 0x00
    if length < 126:
        header.append(masked_bit | length)
    elif length < (1 << 16):
        header.append(masked_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(masked_bit | 127)
        header += struct.pack("!Q", length)
    if mask is None:
        return bytes(header) + payload
    if len(mask) != 4:
        raise ValueError("маска обязана быть длиной 4 байта")
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """Читает один кадр: (последний ли он, опкод, распакованные данные).

    Вынесено на уровень модуля, чтобы этим же кодом можно было читать кадры
    и с серверной стороны — в тестах.
    """
    head = await reader.readexactly(2)
    fin = bool(head[0] & 0x80)
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else None
    payload = await reader.readexactly(length) if length else b""
    if mask is not None:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class WebSocket:
    """Клиентское соединение. Один объект — один сокет."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._send_lock = asyncio.Lock()
        self.closed = False
        self.close_code: int | None = None

    # ---- отправка ----------------------------------------------------------
    async def send(self, message: str | bytes) -> None:
        if self.closed:
            raise ConnectionClosed(self.close_code or 1006)
        if isinstance(message, str):
            payload, opcode = message.encode(), OP_TEXT
        else:
            payload, opcode = message, OP_BINARY
        await self._write(encode_frame(payload, opcode, mask=os.urandom(4)))

    async def _write(self, data: bytes) -> None:
        # блокировка нужна: бот отвечает на ходы из отдельных задач, а два
        # одновременных кадра в одном сокете превратились бы в мусор
        async with self._send_lock:
            self._writer.write(data)
            await self._writer.drain()

    # ---- приём -------------------------------------------------------------
    async def recv(self) -> str | None:
        """Следующее текстовое сообщение или None, если соединение закрыто."""
        fragments: list[bytes] = []
        frame_opcode: int | None = None
        while True:
            try:
                fin, opcode, payload = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionResetError):
                self.closed = True
                return None

            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace")
                self.close_code = code
                if not self.closed:
                    self.closed = True
                    try:
                        await self._write(encode_frame(payload[:2], OP_CLOSE, mask=os.urandom(4)))
                    except Exception:  # noqa: BLE001
                        pass
                self._writer.close()
                if code not in (1000, 1001, 1005):
                    raise ConnectionClosed(code, reason)
                return None
            if opcode == OP_PING:
                await self._write(encode_frame(payload, OP_PONG, mask=os.urandom(4)))
                continue
            if opcode == OP_PONG:
                continue

            if opcode == OP_CONTINUATION:
                if frame_opcode is None:
                    raise WebSocketError("продолжение кадра без начала")
            else:
                frame_opcode = opcode
            fragments.append(payload)

            if fin:
                data = b"".join(fragments)
                if frame_opcode == OP_BINARY:
                    # бинарные кадры сервер не шлёт; пропускаем, чтобы не падать
                    fragments, frame_opcode = [], None
                    continue
                return data.decode("utf-8")

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        return await read_frame(self._reader)

    def __aiter__(self) -> "WebSocket":
        return self

    async def __anext__(self) -> str:
        message = await self.recv()
        if message is None:
            raise StopAsyncIteration
        return message

    # ---- закрытие ----------------------------------------------------------
    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.closed:
            self.closed = True
            payload = struct.pack("!H", code) + reason.encode()
            try:
                await self._write(encode_frame(payload, OP_CLOSE, mask=os.urandom(4)))
            except Exception:  # noqa: BLE001
                pass
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    async def __aenter__(self) -> "WebSocket":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


async def connect(url: str, *, timeout: float = 10.0) -> WebSocket:
    """Открывает соединение и проводит рукопожатие."""
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss"):
        raise ValueError(f"ожидалась схема ws:// или wss://, получено {parts.scheme!r}")
    secure = parts.scheme == "wss"
    host = parts.hostname or "localhost"
    port = parts.port or (443 if secure else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    ssl_context = ssl_module.create_default_context() if secure else None
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=ssl_context), timeout=timeout
    )

    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    writer.write(request.encode())
    await writer.drain()

    status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
    if not status_line:
        raise WebSocketError("сервер закрыл соединение до ответа")
    parts_status = status_line.decode("latin-1").split(" ", 2)
    if len(parts_status) < 2 or parts_status[1] != "101":
        raise WebSocketError(
            f"сервер не согласился на WebSocket: {status_line.decode('latin-1').strip()}"
        )

    headers: dict[str, str] = {}
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if line in (b"\r\n", b"\n", b""):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()

    expected = accept_key(key)
    if headers.get("sec-websocket-accept") != expected:
        raise WebSocketError("сервер прислал неверный Sec-WebSocket-Accept")

    return WebSocket(reader, writer)
