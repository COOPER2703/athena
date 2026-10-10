from __future__ import annotations

import asyncio
import logging
from typing import TypeVar

from conftest import RecordingTraceSink, wait_until
from protocol import SessionEndReason
from server.app.config import AppConfig, GeminiConfig
from server.app.root import App, build_app
from server.core.commands import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    CloseLiveConnection,
    Command,
    OpenLiveConnection,
    SendAudioToClient,
    SendAudioToLlm,
    StartIdleTimer,
)
from server.core.events import (
    ClientAudio,
    ClientRegistered,
    Event,
    LlmAudio,
    LlmOpened,
    SessionRequested,
    TimerFired,
)

_CommandT = TypeVar("_CommandT", bound=Command)


class FakeAdapter:
    """Adaptateur factice exposant le même seam que transport/gemini."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.commands: list[Command] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def recv(self) -> Event:
        return await self.events.get()

    async def execute(self, command: Command) -> None:
        self.commands.append(command)

    async def feed(self, event: Event) -> None:
        await self.events.put(event)

    def of_type(self, command_type: type[_CommandT]) -> list[_CommandT]:
        return [c for c in self.commands if isinstance(c, command_type)]


class FakeClock:
    def __init__(self) -> None:
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self.scheduled: list[tuple[str, float]] = []
        self.cancelled: list[str] = []
        self.closed = False

    def schedule(self, connection_id: str, timeout: float) -> None:
        self.scheduled.append((connection_id, timeout))

    def cancel(self, connection_id: str) -> None:
        self.cancelled.append(connection_id)

    async def recv(self) -> Event:
        return await self.events.get()

    async def fire(self, connection_id: str) -> None:
        await self.events.put(TimerFired(connection_id=connection_id))

    def close(self) -> None:
        self.closed = True


def _build() -> tuple[FakeAdapter, FakeAdapter, FakeClock, RecordingTraceSink, App]:
    transport = FakeAdapter()
    gemini = FakeAdapter()
    clock = FakeClock()
    sink = RecordingTraceSink()
    app = App(
        transport=transport, gemini=gemini, clock=clock, trace_sink=sink
    )
    return transport, gemini, clock, sink, app


async def _open_visible_session(
    transport: FakeAdapter, gemini: FakeAdapter
) -> None:
    await transport.feed(ClientRegistered(client_id="c1"))
    await transport.feed(SessionRequested(client_id="c1"))
    await gemini.feed(LlmOpened())
    await gemini.feed(LlmAudio(b"hi"))
    await wait_until(lambda: transport.of_type(AnnounceSessionStarted))


def test_run_starts_transport_and_fans_in_both_adapters() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)

            await transport.feed(ClientRegistered(client_id="c1"))
            await transport.feed(SessionRequested(client_id="c1"))
            await wait_until(lambda: gemini.of_type(OpenLiveConnection))
            await wait_until(lambda: clock.scheduled)

            assert any(e.kind == "SessionRequested" for e in sink.entries)

            await gemini.feed(LlmOpened())
            await gemini.feed(LlmAudio(b"hello"))
            await wait_until(lambda: transport.of_type(AnnounceSessionStarted))
            assert transport.of_type(SendAudioToClient)[0].data == b"hello"

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_client_audio_during_a_live_session_goes_to_the_llm() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(ClientRegistered(client_id="c1"))
            await transport.feed(SessionRequested(client_id="c1"))
            await gemini.feed(LlmOpened())
            await wait_until(lambda: gemini.of_type(OpenLiveConnection))

            await transport.feed(ClientAudio(data=b"mic"))
            await wait_until(lambda: gemini.of_type(SendAudioToLlm))
            assert gemini.of_type(SendAudioToLlm)[0].data == b"mic"

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_clock_timeout_tears_the_session_down() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(ClientRegistered(client_id="c1"))
            await transport.feed(SessionRequested(client_id="c1"))
            await wait_until(lambda: gemini.of_type(OpenLiveConnection))
            connection_id = gemini.of_type(OpenLiveConnection)[0].connection_id
            await gemini.feed(LlmOpened())

            await clock.fire(connection_id)
            await wait_until(lambda: gemini.of_type(CloseLiveConnection))

            ended = transport.of_type(AnnounceSessionEnded)
            assert ended and ended[0].reason is SessionEndReason.TIMEOUT
            assert connection_id in clock.cancelled

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_idle_timer_is_armed_and_rearmed_through_the_clock() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await transport.feed(SessionRequested(client_id="c1"))
            await wait_until(lambda: clock.scheduled)
            first_connection = clock.scheduled[0][0]

            await gemini.feed(LlmOpened())
            await gemini.feed(LlmAudio(b"one"))
            await wait_until(lambda: len(clock.scheduled) >= 2)
            assert all(key == first_connection for key, _ in clock.scheduled)

            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_shutdown_requested_runs_teardown_with_shutdown_reason() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        await wait_until(lambda: transport.started)
        await _open_visible_session(transport, gemini)

        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)

        ended = transport.of_type(AnnounceSessionEnded)
        assert ended and ended[-1].reason is SessionEndReason.SHUTDOWN
        assert gemini.of_type(CloseLiveConnection)
        assert clock.scheduled[0][0] in clock.cancelled
        assert transport.stopped
        assert clock.closed
        assert any(
            entry.payload.get("reason") == "SHUTDOWN" for entry in sink.entries
        )

    asyncio.run(scenario())


def test_shutdown_while_idle_stops_cleanly() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        await wait_until(lambda: transport.started)

        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)

        assert transport.stopped
        assert clock.closed

    asyncio.run(scenario())


def test_run_leaves_no_orphan_task() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        await wait_until(lambda: transport.started)

        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)

        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
        ]
        assert pending == []

    asyncio.run(scenario())


def test_shutdown_requested_is_traced_even_when_idle() -> None:
    async def scenario() -> None:
        transport, gemini, clock, sink, app = _build()
        runner = asyncio.create_task(app.run())
        await wait_until(lambda: transport.started)

        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)

        assert any(e.kind == "ShutdownRequested" for e in sink.entries)

    asyncio.run(scenario())


def test_build_app_installs_logging_bridge_debug_only_when_flag_on() -> None:
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    gemini = GeminiConfig(api_key="test-key")
    try:
        build_app(AppConfig(gemini=gemini, debug_decisions=True))
        assert root.level == logging.DEBUG

        build_app(AppConfig(gemini=gemini, debug_decisions=False, log_level="WARNING"))
        assert root.level == logging.WARNING
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
