from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import pytest
import websockets
from websockets.asyncio.client import ClientConnection

from protocol import (
    Audio,
    Error,
    Ping,
    Pong,
    Register,
    Registered,
    ToolDeclaration,
    decode,
    encode,
)
from server.adapters.transport import WebSocketTransport
from server.core.commands import SendAudioToClient, Trace
from server.core.events import ClientRegistered
from server.core.ports import TraceEntry


class RecordingTraceSink:
    def __init__(self) -> None:
        self.entries: list[TraceEntry] = []

    def emit(self, entry: TraceEntry) -> None:
        self.entries.append(entry)

    def of_kind(self, kind: str) -> list[TraceEntry]:
        return [entry for entry in self.entries if entry.kind == kind]


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


@asynccontextmanager
async def running_transport(
    *,
    sink: RecordingTraceSink | None = None,
    server_tools: list[str] | None = None,
) -> AsyncIterator[WebSocketTransport]:
    transport = WebSocketTransport(
        host="127.0.0.1",
        port=0,
        server_tools=server_tools,
        trace_sink=sink,
    )
    await transport.start()
    try:
        yield transport
    finally:
        await transport.stop()


def _url(transport: WebSocketTransport) -> str:
    return f"ws://127.0.0.1:{transport.port}"


async def register(
    ws: ClientConnection,
    transport: WebSocketTransport,
    *,
    client_name: str = "desktop",
    platform: str = "linux",
    tools: list[ToolDeclaration] | None = None,
) -> Registered:
    await ws.send(
        encode(
            Register(
                client_name=client_name,
                platform=platform,
                client_tools=tools or [],
            )
        )
    )
    response = decode(await asyncio.wait_for(ws.recv(), 1.0))
    assert isinstance(response, Registered)
    event = await asyncio.wait_for(transport.recv(), 1.0)
    assert isinstance(event, ClientRegistered)
    assert event.client_id == response.client_id
    return response


def test_register_returns_registered_and_enqueues_event() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        async with running_transport(sink=sink, server_tools=["memory"]) as transport:
            async with websockets.connect(_url(transport)) as ws:
                response = await register(
                    ws, transport, client_name="desktop", platform="linux"
                )
                assert response.client_id
                assert response.server_tools == ["memory"]
                assert len(sink.of_kind("client_registered")) == 1

    asyncio.run(scenario())


def test_register_falls_back_to_client_id_and_unknown_platform() -> None:
    async def scenario() -> None:
        async with running_transport() as transport:
            async with websockets.connect(_url(transport)) as ws:
                await ws.send(encode(Register()))
                response = decode(await asyncio.wait_for(ws.recv(), 1.0))
                assert isinstance(response, Registered)
                event = await asyncio.wait_for(transport.recv(), 1.0)
                assert isinstance(event, ClientRegistered)
                assert event.client_name == response.client_id
                assert event.platform == "unknown"

    asyncio.run(scenario())


def test_register_accepts_and_ignores_client_tools() -> None:
    async def scenario() -> None:
        async with running_transport() as transport:
            async with websockets.connect(_url(transport)) as ws:
                response = await register(
                    ws,
                    transport,
                    tools=[ToolDeclaration(name="shell", description="shell")],
                )
                connection = transport.clients[response.client_id]
                assert not hasattr(connection, "client_tools")

    asyncio.run(scenario())


def test_duplicate_client_name_is_refused_with_error_and_1008() -> None:
    async def scenario() -> None:
        async with running_transport() as transport:
            async with (
                websockets.connect(_url(transport)) as first,
                websockets.connect(_url(transport)) as second,
            ):
                registered = await register(first, transport, client_name="desktop")

                await second.send(encode(Register(client_name="desktop")))
                refusal = decode(await asyncio.wait_for(second.recv(), 1.0))
                assert isinstance(refusal, Error)
                assert "desktop" in refusal.message

                await asyncio.wait_for(second.wait_closed(), 1.0)
                assert second.close_code == 1008

                await asyncio.sleep(0.05)
                assert list(transport.clients) == [registered.client_id]
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(transport.recv(), 0.05)

    asyncio.run(scenario())


def test_ping_gets_pong_and_is_not_enqueued_to_core() -> None:
    async def scenario() -> None:
        async with running_transport() as transport:
            async with websockets.connect(_url(transport)) as ws:
                await register(ws, transport)

                await ws.send(encode(Ping()))
                pong = decode(await asyncio.wait_for(ws.recv(), 1.0))
                assert isinstance(pong, Pong)

                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(transport.recv(), 0.05)

    asyncio.run(scenario())


def test_client_pong_is_traced_and_not_enqueued_to_core() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        async with running_transport(sink=sink) as transport:
            async with websockets.connect(_url(transport)) as ws:
                await register(ws, transport)

                await ws.send(encode(Pong()))
                await wait_until(lambda: bool(sink.of_kind("unexpected_pong")))

                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(transport.recv(), 0.05)

    asyncio.run(scenario())


def test_invalid_frame_is_traced_ignored_and_connection_stays_open() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        async with running_transport(sink=sink) as transport:
            async with websockets.connect(_url(transport)) as ws:
                await ws.send(b"\xff\xff not protobuf")
                await ws.send(encode(Register(client_name="desktop")))
                response = decode(await asyncio.wait_for(ws.recv(), 1.0))
                assert isinstance(response, Registered)
                assert sink.of_kind("protocol_error")
                assert ws.close_code is None

    asyncio.run(scenario())


def test_disconnect_removes_client_and_traces() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        async with running_transport(sink=sink) as transport:
            async with websockets.connect(_url(transport)) as ws:
                response = await register(ws, transport)
                assert response.client_id in transport.clients

            await wait_until(lambda: not transport.clients)
            assert sink.of_kind("client_disconnected")

    asyncio.run(scenario())


def test_send_audio_seam_targets_the_client() -> None:
    async def scenario() -> None:
        async with running_transport() as transport:
            async with websockets.connect(_url(transport)) as ws:
                response = await register(ws, transport)

                await transport.send_audio(response.client_id, b"pcm")
                assert decode(await asyncio.wait_for(ws.recv(), 1.0)) == Audio(
                    data=b"pcm"
                )

                await transport.execute(
                    SendAudioToClient(client_id=response.client_id, data=b"more")
                )
                assert decode(await asyncio.wait_for(ws.recv(), 1.0)) == Audio(
                    data=b"more"
                )

    asyncio.run(scenario())


def test_command_for_unknown_client_is_dropped_and_traced() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        async with running_transport(sink=sink) as transport:
            await transport.execute(SendAudioToClient(client_id="missing", data=b"x"))
            assert sink.of_kind("client_not_found")

    asyncio.run(scenario())


def test_trace_command_is_forwarded_to_the_sink() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        async with running_transport(sink=sink) as transport:
            await transport.execute(
                Trace(
                    source="core",
                    kind="SessionRequested",
                    payload={"connection_id": "s1"},
                    level="debug",
                )
            )
            entry = sink.entries[-1]
            assert entry.source == "core"
            assert entry.kind == "SessionRequested"
            assert entry.payload == {"connection_id": "s1"}
            assert entry.level == "debug"

    asyncio.run(scenario())
