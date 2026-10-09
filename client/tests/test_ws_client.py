from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from client import ws_client
from client.config import BackoffConfig
from client.ws_client import WsTransport
from protocol import (
    Audio,
    Interrupt,
    Ping,
    Pong,
    Register,
    Registered,
    SessionStart,
    decode,
    encode,
)


class FakeConnectionClosed(Exception):
    pass


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def send(self, raw: bytes) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)

    async def feed(self, raw: bytes) -> None:
        await self._queue.put(raw)

    async def drop(self) -> None:
        await self._queue.put(None)

    def __aiter__(self) -> "FakeConnection":
        return self

    async def __anext__(self) -> bytes:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


def fake_websockets(connect: Any) -> SimpleNamespace:
    return SimpleNamespace(connect=connect, ConnectionClosed=FakeConnectionClosed)


async def wait_until(predicate: Any, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def test_connect_sends_register(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        conn = FakeConnection()

        async def connect(url: str) -> FakeConnection:
            return conn

        monkeypatch.setattr(ws_client, "websockets", fake_websockets(connect))
        transport = WsTransport("ws://x", client_name="desktop", platform="desktop")

        await transport.connect()

        msg = decode(conn.sent[0])
        assert isinstance(msg, Register)
        assert msg.client_name == "desktop"
        assert msg.platform == "desktop"
        assert msg.client_tools == []
        assert transport.is_connected

        await transport.disconnect()
        assert conn.closed

    asyncio.run(scenario())


def test_ping_is_sent_periodically(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        conn = FakeConnection()

        async def connect(url: str) -> FakeConnection:
            return conn

        monkeypatch.setattr(ws_client, "websockets", fake_websockets(connect))
        transport = WsTransport("ws://x", ping_interval=0.01)

        await transport.connect()
        await wait_until(
            lambda: any(isinstance(decode(s), Ping) for s in conn.sent)
        )
        await transport.disconnect()

    asyncio.run(scenario())


def test_pong_is_ignored_and_connection_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        conn = FakeConnection()

        async def connect(url: str) -> FakeConnection:
            return conn

        monkeypatch.setattr(ws_client, "websockets", fake_websockets(connect))
        transport = WsTransport("ws://x", ping_interval=100.0)

        await transport.connect()
        await conn.feed(encode(Pong()))
        await conn.feed(encode(Registered(client_id="c1")))

        await wait_until(lambda: transport.client_id == "c1")
        assert transport.is_connected
        await transport.disconnect()

    asyncio.run(scenario())


def test_protocol_error_is_logged_and_non_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        conn = FakeConnection()

        async def connect(url: str) -> FakeConnection:
            return conn

        monkeypatch.setattr(ws_client, "websockets", fake_websockets(connect))
        transport = WsTransport("ws://x", ping_interval=100.0)

        await transport.connect()
        await conn.feed(b"\xff\xff\xff")
        await conn.feed(encode(Registered(client_id="c1")))

        await wait_until(lambda: transport.client_id == "c1")
        assert transport.is_connected
        await transport.disconnect()

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())

    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_outgoing_messages_are_encoded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        conn = FakeConnection()

        async def connect(url: str) -> FakeConnection:
            return conn

        monkeypatch.setattr(ws_client, "websockets", fake_websockets(connect))
        transport = WsTransport("ws://x", ping_interval=100.0)

        await transport.connect()
        await transport.send_session_start()
        await transport.send_audio(b"\x01\x02")
        await transport.send_interrupt()

        decoded = [decode(raw) for raw in conn.sent]
        assert isinstance(decoded[1], SessionStart)
        assert isinstance(decoded[2], Audio)
        assert decoded[2].data == b"\x01\x02"
        assert isinstance(decoded[3], Interrupt)
        await transport.disconnect()

    asyncio.run(scenario())


def test_send_without_connection_raises() -> None:
    transport = WsTransport("ws://x")
    with pytest.raises(RuntimeError):
        asyncio.run(transport.send_session_start())


def test_reconnects_after_drop_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        conns = [FakeConnection(), FakeConnection()]
        calls: list[str] = []

        async def connect(url: str) -> FakeConnection:
            calls.append(url)
            return conns[len(calls) - 1]

        monkeypatch.setattr(ws_client, "websockets", fake_websockets(connect))
        transport = WsTransport(
            "ws://x",
            ping_interval=100.0,
            reconnect=BackoffConfig(initial=0.001, maximum=0.004, factor=2.0),
        )
        dropped: list[bool] = []

        async def on_disconnect() -> None:
            dropped.append(True)

        transport.on_disconnect(on_disconnect)

        task = asyncio.create_task(transport.run())
        await wait_until(lambda: len(calls) == 1)

        await conns[0].drop()
        await wait_until(lambda: len(calls) == 2)
        await wait_until(lambda: dropped)

        assert transport.is_connected
        assert isinstance(decode(conns[1].sent[0]), Register)

        await transport.disconnect()
        await asyncio.wait_for(task, timeout=1)
        assert len(calls) == 2

    asyncio.run(scenario())


def test_deliberate_disconnect_does_not_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        conn = FakeConnection()

        async def connect(url: str) -> FakeConnection:
            return conn

        monkeypatch.setattr(ws_client, "websockets", fake_websockets(connect))
        transport = WsTransport("ws://x", ping_interval=100.0)
        called: list[bool] = []

        async def on_disconnect() -> None:
            called.append(True)

        transport.on_disconnect(on_disconnect)
        await transport.connect()
        await transport.disconnect()
        await asyncio.sleep(0.01)

        assert called == []

    asyncio.run(scenario())
