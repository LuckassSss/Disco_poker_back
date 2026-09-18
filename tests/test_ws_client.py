"""Тесты встроенного WebSocket-клиента.

Две части:
  1. кодек кадров сверяется с контрольными примерами прямо из RFC 6455 §5.7 —
     это независимый эталон, а не «что написал, то и проверил»;
  2. полный прогон по настоящему TCP-сокету против маленького эхо-сервера:
     рукопожатие, маскировка, длинные кадры, фрагментация, ping и закрытие.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.ws_client import (  # noqa: E402
    OP_BINARY,
    OP_CLOSE,
    OP_PING,
    OP_TEXT,
    WebSocketError,
    accept_key,
    connect,
    encode_frame,
)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ------------------------------------------------------- контрольные примеры
def test_accept_key_matches_rfc_example():
    """RFC 6455 §1.3: известный ключ → известный ответ."""
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_unmasked_text_frame_matches_rfc():
    """RFC 6455 §5.7: немаскированное «Hello» — 81 05 48 65 6c 6c 6f."""
    assert encode_frame(b"Hello", OP_TEXT) == bytes(
        [0x81, 0x05, 0x48, 0x65, 0x6C, 0x6C, 0x6F]
    )


def test_masked_text_frame_matches_rfc():
    """RFC 6455 §5.7: маскированное «Hello» с маской 37 fa 21 3d."""
    frame = encode_frame(b"Hello", OP_TEXT, mask=bytes([0x37, 0xFA, 0x21, 0x3D]))
    assert frame == bytes(
        [0x81, 0x85, 0x37, 0xFA, 0x21, 0x3D, 0x7F, 0x9F, 0x4D, 0x51, 0x58]
    )


def test_ping_frame_matches_rfc():
    assert encode_frame(b"Hello", OP_PING) == bytes(
        [0x89, 0x05, 0x48, 0x65, 0x6C, 0x6C, 0x6F]
    )


def test_extended_length_headers():
    """Границы кодирования длины: 125 / 126 / 65535 / 65536 байт."""
    assert encode_frame(b"x" * 125, OP_BINARY)[:2] == bytes([0x82, 125])
    assert encode_frame(b"x" * 126, OP_BINARY)[:4] == bytes([0x82, 126]) + struct.pack("!H", 126)
    big = encode_frame(b"x" * 65535, OP_BINARY)
    assert big[:4] == bytes([0x82, 126]) + struct.pack("!H", 65535)
    huge = encode_frame(b"x" * 65536, OP_BINARY)
    assert huge[:10] == bytes([0x82, 127]) + struct.pack("!Q", 65536)


def test_mask_must_be_four_bytes():
    try:
        encode_frame(b"hi", OP_TEXT, mask=b"ab")
    except ValueError:
        return
    raise AssertionError("маска не 4 байта должна отклоняться")


# ------------------------------------------------------------- эхо-сервер
class EchoServer:
    """Крошечный WebSocket-сервер для тестов: повторяет всё, что прислали.

    Понимает маскированные клиентские кадры, отвечает немаскированными —
    ровно как требует RFC от серверной стороны.
    """

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.handshakes: list[dict[str, str]] = []
        self.reject = False

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        headers: dict[str, str] = {}
        request_line = await reader.readline()
        if not request_line:
            return
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        headers["_request"] = request_line.decode("latin-1").strip()
        self.handshakes.append(headers)

        if self.reject:
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        digest = hashlib.sha1((headers.get("sec-websocket-key", "") + GUID).encode()).digest()
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + base64.b64encode(digest) + b"\r\n\r\n"
        )
        await writer.drain()

        try:
            while True:
                fin, opcode, payload = await self._read(reader)
                if opcode == OP_CLOSE:
                    writer.write(encode_frame(payload[:2], OP_CLOSE))
                    await writer.drain()
                    break
                if opcode == OP_PING:
                    continue
                text = payload.decode("utf-8", "replace")
                if text == "__ping__":
                    writer.write(encode_frame(b"pong-check", OP_PING))
                    writer.write(encode_frame("после пинга".encode(), OP_TEXT))
                elif text == "__fragmented__":
                    writer.write(bytes([0x01, 0x03]) + b"abc")   # первый кадр, FIN=0
                    writer.write(bytes([0x00, 0x03]) + b"def")   # продолжение
                    writer.write(bytes([0x80, 0x03]) + b"ghi")   # последний
                elif text == "__big__":
                    writer.write(encode_frame(("я" * 50_000).encode(), OP_TEXT))
                else:
                    writer.write(encode_frame(payload, OP_TEXT))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _read(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
        head = await reader.readexactly(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        assert masked, "клиент ОБЯЗАН маскировать кадры (RFC 6455 §5.1)"
        mask = await reader.readexactly(4)
        payload = await reader.readexactly(length) if length else b""
        return fin, opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _with_server(scenario) -> None:
    async def run() -> None:
        server = EchoServer()
        await server.start()
        try:
            await scenario(server)
        finally:
            await server.stop()

    asyncio.run(asyncio.wait_for(run(), timeout=20))


# ----------------------------------------------------------------- сценарии
def test_roundtrip_text():
    async def scenario(server: EchoServer) -> None:
        ws = await connect(server.url)
        await ws.send('{"t":"ping","d":{}}')
        assert await ws.recv() == '{"t":"ping","d":{}}'
        await ws.send("кириллица и эмодзи не ломаются")
        assert await ws.recv() == "кириллица и эмодзи не ломаются"
        await ws.close()

    _with_server(scenario)


def test_handshake_sends_required_headers():
    async def scenario(server: EchoServer) -> None:
        ws = await connect(server.url + "?token=abc")
        await ws.close()
        handshake = server.handshakes[-1]
        assert "?token=abc" in handshake["_request"], "query-параметры должны доходить"
        assert handshake["upgrade"].lower() == "websocket"
        assert "upgrade" in handshake["connection"].lower()
        assert handshake["sec-websocket-version"] == "13"
        assert len(base64.b64decode(handshake["sec-websocket-key"])) == 16

    _with_server(scenario)


def test_large_message_uses_extended_length():
    async def scenario(server: EchoServer) -> None:
        ws = await connect(server.url)
        await ws.send("__big__")
        message = await ws.recv()
        assert message == "я" * 50_000
        # и в обратную сторону: длинный кадр от клиента
        payload = "x" * 70_000
        await ws.send(payload)
        assert await ws.recv() == payload
        await ws.close()

    _with_server(scenario)


def test_fragmented_message_is_assembled():
    async def scenario(server: EchoServer) -> None:
        ws = await connect(server.url)
        await ws.send("__fragmented__")
        assert await ws.recv() == "abcdefghi"
        await ws.close()

    _with_server(scenario)


def test_ping_is_answered_and_does_not_break_stream():
    async def scenario(server: EchoServer) -> None:
        ws = await connect(server.url)
        await ws.send("__ping__")
        assert await ws.recv() == "после пинга", "ping не должен подменять сообщение"
        await ws.close()

    _with_server(scenario)


def test_iteration_stops_on_close():
    async def scenario(server: EchoServer) -> None:
        ws = await connect(server.url)
        await ws.send("раз")
        received = []
        async for message in ws:
            received.append(message)
            await ws.close()
        assert received == ["раз"]

    _with_server(scenario)


def test_concurrent_sends_do_not_corrupt_stream():
    """Бот отвечает из отдельных задач — кадры не должны перемешаться."""

    async def scenario(server: EchoServer) -> None:
        ws = await connect(server.url)
        expected = {f"сообщение-{i}" * 40 for i in range(25)}
        await asyncio.gather(*(ws.send(m) for m in expected))
        got = {await ws.recv() for _ in expected}
        assert got == expected
        await ws.close()

    _with_server(scenario)


def test_rejected_handshake_raises_clear_error():
    async def scenario(server: EchoServer) -> None:
        server.reject = True
        try:
            await connect(server.url)
        except WebSocketError as exc:
            assert "401" in str(exc), f"ошибка должна называть причину: {exc}"
            return
        raise AssertionError("отказ сервера должен приводить к понятной ошибке")

    _with_server(scenario)


def test_bad_scheme_rejected():
    async def scenario(server: EchoServer) -> None:
        try:
            await connect(f"http://127.0.0.1:{server.port}/ws")
        except ValueError:
            return
        raise AssertionError("схема http:// должна отклоняться")

    _with_server(scenario)


if __name__ == "__main__":
    from tests.helpers import run_module

    sys.exit(run_module(sys.modules[__name__]))
