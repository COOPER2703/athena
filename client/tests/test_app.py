from __future__ import annotations

import asyncio
from typing import Any

from client.app import ClientApp
from client.config import AppConfig
from client.state import ClientState
from protocol import SessionEnded, SessionStarted


class FakeAudio:
    def __init__(self) -> None:
        self.started_input = False
        self.started_output = False
        self.written: list[bytes] = []
        self.closed = False

    async def start_input(self) -> None:
        self.started_input = True

    async def start_output(self) -> None:
        self.started_output = True

    async def read_chunk(self) -> bytes:
        return b""

    async def write_chunk(self, data: bytes) -> None:
        self.written.append(data)

    async def close(self) -> None:
        self.closed = True


class BlockingFakeAudio(FakeAudio):
    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()

    async def read_chunk(self) -> bytes:
        await self._gate.wait()
        return b""


class FakeTransport:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.sent_audio: list[bytes] = []
        self.session_starts = 0
        self.session_ends = 0
        self.interrupts = 0
        self.connected = False
        self.disconnected = False

    @property
    def client_id(self) -> str | None:
        return "test-client"

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def run(self) -> None:
        self.connected = True
        await asyncio.Event().wait()

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_audio(self, data: bytes) -> None:
        self.sent_audio.append(data)

    async def send_session_start(self) -> None:
        self.session_starts += 1

    async def send_session_end(self) -> None:
        self.session_ends += 1

    async def send_interrupt(self) -> None:
        self.interrupts += 1

    def on_audio(self, handler: Any) -> None:
        self.handlers["audio"] = handler

    def on_session_started(self, handler: Any) -> None:
        self.handlers["session_started"] = handler

    def on_session_ended(self, handler: Any) -> None:
        self.handlers["session_ended"] = handler

    def on_notification(self, handler: Any) -> None:
        self.handlers["notification"] = handler

    def on_text(self, handler: Any) -> None:
        self.handlers["text"] = handler

    def on_error(self, handler: Any) -> None:
        self.handlers["error"] = handler

    def on_disconnect(self, handler: Any) -> None:
        self.handlers["disconnect"] = handler


def make_app(
    *,
    cfg: AppConfig | None = None,
    audio: FakeAudio | None = None,
    transport: FakeTransport | None = None,
) -> ClientApp:
    return ClientApp(
        cfg or AppConfig(),
        audio=audio or FakeAudio(),
        transport=transport or FakeTransport(),
    )


def test_callbacks_are_registered_on_transport() -> None:
    transport = FakeTransport()
    make_app(transport=transport)
    assert set(transport.handlers) == {
        "audio",
        "session_started",
        "session_ended",
        "notification",
        "text",
        "error",
        "disconnect",
    }


def test_starts_listening() -> None:
    assert make_app().state is ClientState.LISTENING


def test_enter_sends_session_start() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app.enter()
        assert transport.session_starts == 1

    asyncio.run(scenario())


def test_session_started_moves_to_active() -> None:
    async def scenario() -> None:
        app = make_app()
        await app._handle_session_started(SessionStarted())
        assert app.state is ClientState.ACTIVE

    asyncio.run(scenario())


def test_session_ended_returns_to_listening() -> None:
    async def scenario() -> None:
        app = make_app()
        await app._handle_session_started(SessionStarted())
        await app._handle_session_ended(SessionEnded())
        assert app.state is ClientState.LISTENING

    asyncio.run(scenario())


def test_reenter_while_active_interrupts_and_flushes_playback() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app._handle_session_started(SessionStarted())
        await app._handle_audio(b"\x01")
        await app._handle_audio(b"\x02")

        await app.enter()

        assert transport.interrupts == 1
        assert transport.session_starts == 0
        assert app._playback.empty()

    asyncio.run(scenario())


def test_enter_while_listening_does_not_interrupt() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app.enter()
        assert transport.interrupts == 0

    asyncio.run(scenario())


def test_audio_not_forwarded_while_listening() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app._forward_audio(b"\x01\x02")
        assert transport.sent_audio == []

    asyncio.run(scenario())


def test_audio_forwarded_while_active() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app._handle_session_started(SessionStarted())
        await app._forward_audio(b"\x01\x02")
        assert transport.sent_audio == [b"\x01\x02"]

    asyncio.run(scenario())


def test_empty_chunk_is_ignored() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app._handle_session_started(SessionStarted())
        await app._forward_audio(b"")
        assert transport.sent_audio == []

    asyncio.run(scenario())


def test_server_audio_is_queued_for_playback() -> None:
    async def scenario() -> None:
        app = make_app()
        await app._handle_audio(b"\x03\x04")
        assert await app._playback.get() == b"\x03\x04"

    asyncio.run(scenario())


def test_client_never_sends_session_end() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app.enter()
        await app._handle_session_started(SessionStarted())
        await app._handle_session_ended(SessionEnded())
        assert transport.session_ends == 0

    asyncio.run(scenario())


def test_disconnect_returns_to_listening() -> None:
    async def scenario() -> None:
        app = make_app()
        await app._handle_session_started(SessionStarted())
        await app._handle_disconnect()
        assert app.state is ClientState.LISTENING

    asyncio.run(scenario())


def test_run_starts_audio_and_tears_down_on_stop() -> None:
    async def scenario() -> None:
        audio = BlockingFakeAudio()
        transport = FakeTransport()
        app = make_app(audio=audio, transport=transport)

        run_task = asyncio.create_task(app.run())
        await asyncio.sleep(0.02)
        assert audio.started_input
        assert audio.started_output
        assert transport.connected

        app.stop()
        await asyncio.wait_for(run_task, timeout=1)

        assert audio.closed
        assert transport.disconnected

    asyncio.run(scenario())
