from __future__ import annotations

import asyncio

import pytest
import websockets

from conftest import RecordingTraceSink, _url, register, running_transport, wait_until
from protocol import (
    Audio,
    Interrupt,
    Ping,
    Pong,
    Registered,
    SessionEnd,
    SessionEndReason,
    SessionEnded,
    SessionStart,
    SessionStarted,
    ToolResult,
    decode,
    encode,
)
from server.adapters.transport import WebSocketTransport
from server.core.commands import (
    CancelIdleTimer,
    CloseLiveConnection,
    Command,
    OpenLiveConnection,
    SendAudioToClient,
    SendAudioToLlm,
    StartIdleTimer,
)
from server.core.coordinator import Coordinator
from server.core.events import (
    ClientAudio,
    ClientInterrupt,
    ClientRegistered,
    ClientToolResult,
    Event,
    LlmAudio,
    LlmClosed,
    LlmOpened,
)


def _produced(commands: list[Command], command_type: type) -> bool:
    return any(isinstance(command, command_type) for command in commands)


async def _drain_registered(transport: WebSocketTransport, registered: Registered) -> None:
    event = await asyncio.wait_for(transport.recv(), 1.0)
    assert isinstance(event, ClientRegistered)
    assert event.client_id == registered.client_id


async def _core_loop(
    transport: WebSocketTransport, coordinator: Coordinator, seen: list[Command]
) -> None:
    """The tiny ownership loop ticket #6 will use."""
    while True:
        event = await transport.recv()
        for command in coordinator.handle(event):
            seen.append(command)
            await transport.execute(command)


async def _deliver(
    transport: WebSocketTransport,
    coordinator: Coordinator,
    event: Event,
    seen: list[Command],
) -> None:
    """Feed an LLM-side event as a Gemini adapter would, through the same seam."""
    for command in coordinator.handle(event):
        seen.append(command)
        await transport.execute(command)


def test_session_start_drives_real_core_and_wire_sequence() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        async with running_transport(sink=sink) as transport:
            coordinator = Coordinator()
            seen: list[Command] = []
            runner = asyncio.create_task(_core_loop(transport, coordinator, seen))
            try:
                async with websockets.connect(_url(transport)) as ws:
                    registered = await register(
                        ws, transport, client_name="desktop", drain=False
                    )

                    await ws.send(encode(SessionStart()))
                    await wait_until(
                        lambda: _produced(seen, OpenLiveConnection)
                        and _produced(seen, StartIdleTimer)
                    )
                    await wait_until(
                        lambda: any(
                            entry.kind == "SessionRequested" for entry in sink.entries
                        )
                    )

                    # Opening the Live connection and arming the timer are not the
                    # transport's business: nothing reaches the client yet.
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), 0.05)

                    # The core opens the Live connection and starts producing.
                    await _deliver(transport, coordinator, LlmOpened(), seen)
                    await _deliver(transport, coordinator, LlmAudio(b"reply-1"), seen)

                    assert (
                        decode(await asyncio.wait_for(ws.recv(), 1.0))
                        == SessionStarted(auto=False)
                    )
                    assert (
                        decode(await asyncio.wait_for(ws.recv(), 1.0))
                        == Audio(data=b"reply-1")
                    )

                    # Downlink client audio in a live Session goes to the LLM, not
                    # back to the client.
                    await ws.send(encode(Audio(data=b"mic")))
                    await wait_until(lambda: _produced(seen, SendAudioToLlm))
                    with pytest.raises(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), 0.05)

                    # The Live connection closes -> the Session is announced ended.
                    await _deliver(transport, coordinator, LlmClosed(), seen)
                    assert (
                        decode(await asyncio.wait_for(ws.recv(), 1.0))
                        == SessionEnded(reason=SessionEndReason.NORMAL)
                    )

                    # Non-transport commands were produced and silently ignored by
                    # the adapter (no wire message, no crash).
                    assert registered.client_id in transport.clients
                    assert _produced(seen, OpenLiveConnection)
                    assert _produced(seen, StartIdleTimer)
                    assert _produced(seen, SendAudioToLlm)
                    assert _produced(seen, CancelIdleTimer)
                    assert _produced(seen, CloseLiveConnection)
            finally:
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_targeted_command_reaches_only_the_addressed_client() -> None:
    async def scenario() -> None:
        async with running_transport() as transport:
            async with (
                websockets.connect(_url(transport)) as ws_a,
                websockets.connect(_url(transport)) as ws_b,
            ):
                a = await register(ws_a, transport, client_name="a", drain=False)
                b = await register(ws_b, transport, client_name="b", drain=False)

                await transport.execute(
                    SendAudioToClient(client_id=a.client_id, data=b"to-a")
                )
                assert decode(await asyncio.wait_for(ws_a.recv(), 1.0)) == Audio(
                    data=b"to-a"
                )
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws_b.recv(), 0.05)

                # Same guarantee through the ClientLink seam.
                await transport.send_audio(b.client_id, b"to-b")
                assert decode(await asyncio.wait_for(ws_b.recv(), 1.0)) == Audio(
                    data=b"to-b"
                )
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws_a.recv(), 0.05)

                await transport.announce_session_started(a.client_id, auto=True)
                assert decode(await asyncio.wait_for(ws_a.recv(), 1.0)) == (
                    SessionStarted(auto=True)
                )
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws_b.recv(), 0.05)

                await transport.announce_session_ended(
                    b.client_id, SessionEndReason.TIMEOUT
                )
                assert decode(await asyncio.wait_for(ws_b.recv(), 1.0)) == (
                    SessionEnded(reason=SessionEndReason.TIMEOUT)
                )
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws_a.recv(), 0.05)

    asyncio.run(scenario())


def test_inbound_audio_and_interrupt_translate_over_a_real_socket() -> None:
    async def scenario() -> None:
        async with running_transport() as transport:
            async with websockets.connect(_url(transport)) as ws:
                registered = await register(
                    ws, transport, client_name="desktop", drain=False
                )
                await _drain_registered(transport, registered)

                await ws.send(encode(Audio(data=b"pcm")))
                event = await asyncio.wait_for(transport.recv(), 1.0)
                assert event == ClientAudio(data=b"pcm")

                await ws.send(encode(Interrupt()))
                event = await asyncio.wait_for(transport.recv(), 1.0)
                assert event == ClientInterrupt(client_id=registered.client_id)

    asyncio.run(scenario())


def test_tool_result_is_translated_enqueued_and_keeps_connection_alive() -> None:
    async def scenario() -> None:
        async with running_transport() as transport:
            async with websockets.connect(_url(transport)) as ws:
                registered = await register(
                    ws, transport, client_name="desktop", drain=False
                )
                await _drain_registered(transport, registered)

                await ws.send(
                    encode(ToolResult(id="t1", name="shell", result_json=b"{}"))
                )
                event = await asyncio.wait_for(transport.recv(), 1.0)
                assert event == ClientToolResult(
                    tool_id="t1", name="shell", result_json=b"{}"
                )

                await ws.send(encode(Ping()))
                assert isinstance(
                    decode(await asyncio.wait_for(ws.recv(), 1.0)), Pong
                )

    asyncio.run(scenario())


def test_session_end_is_dropped_without_crashing_the_connection() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        async with running_transport(sink=sink) as transport:
            async with websockets.connect(_url(transport)) as ws:
                registered = await register(
                    ws, transport, client_name="desktop", drain=False
                )
                await _drain_registered(transport, registered)

                await ws.send(encode(SessionEnd()))
                await wait_until(lambda: bool(sink.of_kind("unhandled_message")))

                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(transport.recv(), 0.05)

                # The connection is still usable after the dropped message.
                await ws.send(encode(Ping()))
                assert isinstance(
                    decode(await asyncio.wait_for(ws.recv(), 1.0)), Pong
                )

    asyncio.run(scenario())
