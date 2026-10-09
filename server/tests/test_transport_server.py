from __future__ import annotations

import asyncio

import pytest
import websockets

from conftest import RecordingTraceSink, _url, register, running_transport, wait_until
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
from server.core.commands import (
    CloseLiveConnection,
    OpenLiveConnection,
    SendAudioToClient,
    Trace,
)
from server.core.events import ClientRegistered


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


def test_non_transport_command_is_ignored_without_crashing() -> None:
    async def scenario() -> None:
        async with running_transport() as transport:
            async with websockets.connect(_url(transport)) as ws:
                response = await register(ws, transport)

                # Reserved seams: T1 core emits none of these, and the adapter
                # must ignore them (no wire message, no crash, connection open).
                await transport.execute(OpenLiveConnection(connection_id="s1"))
                await transport.execute(CloseLiveConnection(connection_id="s1"))

                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), 0.05)
                assert response.client_id in transport.clients

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
