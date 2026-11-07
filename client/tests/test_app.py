from __future__ import annotations

import asyncio
from typing import Any

import pytest

from client.app import ClientApp
from client.config import AppConfig, WakeWordConfig
from client.state import ClientState
from client.wake_word import Detection
from client.ws_client import AccessTokenRefusedError
from protocol import SessionEnded, SessionStarted


class FakeAudio:
    def __init__(self) -> None:
        self.started_input = False
        self.started_output = False
        self.written: list[bytes] = []
        self.flushed = False
        self.closed = False

    async def start_input(self) -> None:
        self.started_input = True

    async def start_output(self) -> None:
        self.started_output = True

    async def read_chunk(self) -> bytes:
        return b""

    async def write_chunk(self, data: bytes) -> None:
        self.written.append(data)

    async def flush(self) -> None:
        self.flushed = True

    async def close(self) -> None:
        self.closed = True


class BlockingFakeAudio(FakeAudio):
    def __init__(self) -> None:
        super().__init__()
        self._gate = asyncio.Event()

    async def read_chunk(self) -> bytes:
        await self._gate.wait()
        return b""


class FakeDetector:
    def __init__(
        self, detections: dict[bytes, Detection] | None = None
    ) -> None:
        self._detections = detections or {}
        self.processed: list[bytes] = []
        self.closed = False

    def process(self, chunk: bytes) -> Detection | None:
        self.processed.append(chunk)
        return self._detections.get(chunk)

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self) -> None:
        self.listener: Any = None
        self.sent_audio: list[bytes] = []
        self.session_starts = 0
        self.interrupts = 0
        self.connected = False
        self.disconnected = False
        self.events: list[tuple[str, bytes | None]] = []

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
        self.events.append(("audio", data))

    async def send_session_start(self) -> None:
        self.session_starts += 1
        self.events.append(("session_start", None))

    async def send_interrupt(self) -> None:
        self.interrupts += 1

    def set_listener(self, listener: Any) -> None:
        self.listener = listener


async def wait_until(predicate: Any, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


def make_app(
    *,
    cfg: AppConfig | None = None,
    audio: FakeAudio | None = None,
    transport: FakeTransport | None = None,
    detector: FakeDetector | None = None,
) -> ClientApp:
    return ClientApp(
        cfg or AppConfig(),
        audio=audio or FakeAudio(),
        transport=transport or FakeTransport(),
        detector=detector,
    )


def test_listener_is_registered_on_transport() -> None:
    transport = FakeTransport()
    app = make_app(transport=transport)
    assert transport.listener is app


def test_starts_listening() -> None:
    assert make_app().state is ClientState.LISTENING


def test_tap_sends_session_start() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app.tap()
        assert transport.session_starts == 1

    asyncio.run(scenario())


def test_tap_puts_client_active() -> None:
    async def scenario() -> None:
        app = make_app()
        await app.tap()
        assert app.state is ClientState.ACTIVE

    asyncio.run(scenario())


def test_audio_forwarded_after_tap() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app.tap()
        await app.forward_audio(b"\x01\x02")
        assert transport.sent_audio == [b"\x01\x02"]

    asyncio.run(scenario())


def test_session_started_moves_to_active() -> None:
    async def scenario() -> None:
        app = make_app()
        await app.on_session_started(SessionStarted())
        assert app.state is ClientState.ACTIVE

    asyncio.run(scenario())


def test_session_ended_returns_to_listening() -> None:
    async def scenario() -> None:
        app = make_app()
        await app.on_session_started(SessionStarted())
        await app.on_session_ended(SessionEnded())
        assert app.state is ClientState.LISTENING

    asyncio.run(scenario())


def test_reenter_while_active_interrupts_and_flushes_playback() -> None:
    async def scenario() -> None:
        audio = FakeAudio()
        transport = FakeTransport()
        app = make_app(audio=audio, transport=transport)
        await app.on_session_started(SessionStarted())
        await app.on_audio(b"\x01")
        await app.on_audio(b"\x02")

        await app.tap()

        assert transport.interrupts == 1
        assert transport.session_starts == 0
        assert audio.flushed
        assert app._playback.empty()

    asyncio.run(scenario())


def test_tap_while_listening_does_not_interrupt() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app.tap()
        assert transport.interrupts == 0

    asyncio.run(scenario())


def test_audio_not_forwarded_while_listening() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app.forward_audio(b"\x01\x02")
        assert transport.sent_audio == []

    asyncio.run(scenario())


def test_audio_forwarded_while_active() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app.on_session_started(SessionStarted())
        await app.forward_audio(b"\x01\x02")
        assert transport.sent_audio == [b"\x01\x02"]

    asyncio.run(scenario())


def test_empty_chunk_is_ignored() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        app = make_app(transport=transport)
        await app.on_session_started(SessionStarted())
        await app.forward_audio(b"")
        assert transport.sent_audio == []

    asyncio.run(scenario())


def test_server_audio_is_written_to_output() -> None:
    async def scenario() -> None:
        audio = BlockingFakeAudio()
        app = make_app(audio=audio)

        run_task = asyncio.create_task(app.run())
        await wait_until(lambda: audio.started_output)

        await app.on_audio(b"\x03\x04")
        await wait_until(lambda: audio.written == [b"\x03\x04"])

        app.stop()
        await asyncio.wait_for(run_task, timeout=1)

    asyncio.run(scenario())


def test_wake_word_detection_starts_a_session_with_the_pre_roll() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector(
            {b"trigger": Detection(score=0.87, pre_roll=b"before-trigger")}
        )
        app = make_app(transport=transport, detector=detector)

        await app.forward_audio(b"trigger")
        await app.forward_audio(b"live")

        assert transport.session_starts == 1
        assert app.state is ClientState.ACTIVE
        assert transport.events == [
            ("session_start", None),
            ("audio", b"before-trigger"),
            ("audio", b"live"),
        ]
        assert detector.processed == [b"trigger"]

    asyncio.run(scenario())


def test_audio_before_a_detection_is_only_fed_to_the_detector() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector()
        app = make_app(transport=transport, detector=detector)

        await app.forward_audio(b"silence")

        assert detector.processed == [b"silence"]
        assert transport.session_starts == 0
        assert transport.sent_audio == []
        assert app.state is ClientState.LISTENING

    asyncio.run(scenario())


def test_detector_is_not_consulted_while_active() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        detector = FakeDetector()
        app = make_app(transport=transport, detector=detector)
        await app.on_session_started(SessionStarted())

        await app.forward_audio(b"live")

        assert detector.processed == []
        assert transport.sent_audio == [b"live"]

    asyncio.run(scenario())


def test_offline_detection_is_ignored_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        detector = FakeDetector({b"trigger": Detection(score=0.91)})
        app = make_app(transport=transport, detector=detector)

        with caplog.at_level("WARNING", logger="athena.client"):
            await app.forward_audio(b"trigger")

        assert transport.session_starts == 0
        assert transport.sent_audio == []
        assert app.state is ClientState.LISTENING

    asyncio.run(scenario())

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING"
    ]
    assert any("hors connexion" in message for message in warnings)
    assert any("0.910" in message for message in warnings)


def test_disabled_wake_word_never_consults_the_detector() -> None:
    async def scenario() -> None:
        cfg = AppConfig(wake_word=WakeWordConfig(enabled=False))
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector({b"trigger": Detection(score=0.99)})
        app = make_app(cfg=cfg, transport=transport, detector=detector)

        await app.forward_audio(b"trigger")
        await app.tap()

        assert detector.processed == []
        assert transport.session_starts == 1
        assert app.state is ClientState.ACTIVE

    asyncio.run(scenario())


def test_detection_is_logged_with_its_score(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector({b"trigger": Detection(score=0.87)})
        app = make_app(transport=transport, detector=detector)

        with caplog.at_level("INFO", logger="athena.client"):
            await app.forward_audio(b"trigger")

    asyncio.run(scenario())

    infos = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "INFO"
    ]
    assert any(
        "Wake word détecté" in message and "0.870" in message
        for message in infos
    )


def test_detector_is_closed_when_the_client_stops() -> None:
    async def scenario() -> None:
        audio = BlockingFakeAudio()
        detector = FakeDetector()
        app = make_app(audio=audio, detector=detector)

        run_task = asyncio.create_task(app.run())
        await wait_until(lambda: audio.started_output)

        app.stop()
        await asyncio.wait_for(run_task, timeout=1)

        assert detector.closed

    asyncio.run(scenario())


def test_disconnect_returns_to_listening() -> None:
    async def scenario() -> None:
        app = make_app()
        await app.on_session_started(SessionStarted())
        await app.on_disconnect()
        assert app.state is ClientState.LISTENING

    asyncio.run(scenario())


def test_run_propagates_an_access_token_refusal_after_cleanup() -> None:
    class RefusingTransport(FakeTransport):
        async def run(self) -> None:
            raise AccessTokenRefusedError("Jeton d'accès refusé (401)")

    async def scenario() -> None:
        audio = BlockingFakeAudio()
        transport = RefusingTransport()
        app = make_app(audio=audio, transport=transport)

        with pytest.raises(AccessTokenRefusedError):
            await app.run()
        assert audio.closed
        assert transport.disconnected

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
