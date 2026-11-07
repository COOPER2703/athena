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
    clock: Any = None,
) -> ClientApp:
    return ClientApp(
        cfg or AppConfig(),
        audio=audio or FakeAudio(),
        transport=transport or FakeTransport(),
        detector=detector,
        clock=clock,
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


def test_timeout_rearms_with_cooldown_then_detects_again() -> None:
    async def scenario() -> None:
        now = [100.0]
        cfg = AppConfig(
            wake_word=WakeWordConfig(
                cooldown_seconds=2.0, session_start_timeout_seconds=0.02
            )
        )
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector({b"trigger": Detection(score=0.9)})
        app = make_app(
            cfg=cfg,
            transport=transport,
            detector=detector,
            clock=lambda: now[0],
        )

        await app.forward_audio(b"trigger")
        await wait_until(lambda: app.state is ClientState.LISTENING)

        # Le timeout est une fin de Session comme une autre : cooldown armé.
        await app.forward_audio(b"trigger")
        assert detector.processed == [b"trigger"]
        assert transport.session_starts == 1

        now[0] += 2.001
        await app.forward_audio(b"trigger")
        assert detector.processed == [b"trigger", b"trigger"]
        assert transport.session_starts == 2
        await app.on_session_started(SessionStarted())

    asyncio.run(scenario())


def test_teardown_leaves_no_timeout_task_alive() -> None:
    async def scenario() -> None:
        audio = BlockingFakeAudio()
        app = make_app(audio=audio)

        run_task = asyncio.create_task(app.run())
        await wait_until(lambda: audio.started_output)

        await app.tap()
        assert app._session_timeout_task is not None

        app.stop()
        await asyncio.wait_for(run_task, timeout=1)

        assert app._session_timeout_task is None
        assert not [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "athena-client-session-timeout"
        ]

    asyncio.run(scenario())


def test_detector_close_failure_does_not_break_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ExplodingCloseDetector(FakeDetector):
        def close(self) -> None:
            raise RuntimeError("fermeture cassée")

    async def scenario() -> None:
        audio = BlockingFakeAudio()
        transport = FakeTransport()
        app = make_app(
            audio=audio, transport=transport, detector=ExplodingCloseDetector()
        )

        run_task = asyncio.create_task(app.run())
        await wait_until(lambda: audio.started_output)

        app.stop()
        await asyncio.wait_for(run_task, timeout=1)

        assert audio.closed
        assert transport.disconnected

    asyncio.run(scenario())

    errors = [
        record for record in caplog.records if record.levelname == "ERROR"
    ]
    assert errors
    assert errors[0].exc_info is not None
    assert "fermeture cassée" in str(errors[0].exc_info[1])


def test_detector_failure_disables_voice_but_keeps_push_to_talk(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ExplodingDetector(FakeDetector):
        def process(self, chunk: bytes) -> Detection | None:
            self.processed.append(chunk)
            raise RuntimeError("inférence cassée")

    async def scenario() -> None:
        transport = FakeTransport()
        transport.connected = True
        detector = ExplodingDetector()
        cfg = AppConfig(
            wake_word=WakeWordConfig(session_start_timeout_seconds=0.0)
        )
        app = make_app(cfg=cfg, transport=transport, detector=detector)

        with caplog.at_level("ERROR", logger="athena.client"):
            await app.forward_audio(b"trigger")

        assert app.state is ClientState.LISTENING
        assert transport.session_starts == 0

        # Le détecteur n'est plus jamais consulté.
        await app.forward_audio(b"after-failure")
        assert detector.processed == [b"trigger"]

        # Le push-to-talk reste pleinement fonctionnel.
        await app.tap()
        assert app.state is ClientState.ACTIVE
        assert transport.session_starts == 1
        await app.forward_audio(b"live")
        assert transport.sent_audio == [b"live"]

    asyncio.run(scenario())

    errors = [
        record for record in caplog.records if record.levelname == "ERROR"
    ]
    assert errors
    assert errors[0].exc_info is not None
    assert "inférence cassée" in str(errors[0].exc_info[1])


def test_session_ended_cancels_the_phantom_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        cfg = AppConfig(
            wake_word=WakeWordConfig(
                cooldown_seconds=0.0, session_start_timeout_seconds=0.02
            )
        )
        app = make_app(cfg=cfg)

        await app.tap()
        await app.on_session_ended(SessionEnded())
        assert app._session_timeout_task is None
        await asyncio.sleep(0.06)

        assert app.state is ClientState.LISTENING

    asyncio.run(scenario())

    assert not any(
        "Session fantôme" in record.getMessage() for record in caplog.records
    )


def test_disconnect_cancels_timeout_and_applies_cooldown() -> None:
    async def scenario() -> None:
        now = [50.0]
        cfg = AppConfig(wake_word=WakeWordConfig(cooldown_seconds=2.0))
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector({b"trigger": Detection(score=0.9)})
        app = make_app(
            cfg=cfg,
            transport=transport,
            detector=detector,
            clock=lambda: now[0],
        )

        await app.tap()
        await app.on_disconnect()

        assert app.state is ClientState.LISTENING
        assert app._session_timeout_task is None
        await app.forward_audio(b"trigger")
        assert detector.processed == []

        now[0] += 2.001
        await app.forward_audio(b"trigger")
        assert detector.processed == [b"trigger"]

    asyncio.run(scenario())


def test_server_audio_cancels_the_phantom_timeout() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector({b"trigger": Detection(score=0.9)})
        cfg = AppConfig(
            wake_word=WakeWordConfig(
                cooldown_seconds=0.0, session_start_timeout_seconds=0.02
            )
        )
        app = make_app(cfg=cfg, transport=transport, detector=detector)

        await app.forward_audio(b"trigger")
        await app.on_audio(b"athena-speaks")
        await asyncio.sleep(0.06)

        assert app.state is ClientState.ACTIVE

    asyncio.run(scenario())


def test_session_started_cancels_the_phantom_timeout() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector({b"trigger": Detection(score=0.9)})
        cfg = AppConfig(
            wake_word=WakeWordConfig(
                cooldown_seconds=0.0, session_start_timeout_seconds=0.02
            )
        )
        app = make_app(cfg=cfg, transport=transport, detector=detector)

        await app.forward_audio(b"trigger")
        await app.on_session_started(SessionStarted())
        await asyncio.sleep(0.06)

        assert app.state is ClientState.ACTIVE

    asyncio.run(scenario())


def test_tap_without_server_confirmation_returns_to_listening() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        cfg = AppConfig(
            wake_word=WakeWordConfig(
                cooldown_seconds=0.0, session_start_timeout_seconds=0.02
            )
        )
        app = make_app(cfg=cfg, transport=transport)

        await app.tap()
        assert app.state is ClientState.ACTIVE

        await wait_until(lambda: app.state is ClientState.LISTENING)

    asyncio.run(scenario())


def test_wake_word_without_server_confirmation_returns_to_listening(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector({b"trigger": Detection(score=0.9)})
        cfg = AppConfig(
            wake_word=WakeWordConfig(
                cooldown_seconds=0.0, session_start_timeout_seconds=0.02
            )
        )
        app = make_app(cfg=cfg, transport=transport, detector=detector)

        with caplog.at_level("WARNING", logger="athena.client"):
            await app.forward_audio(b"trigger")
            assert app.state is ClientState.ACTIVE

            await wait_until(lambda: app.state is ClientState.LISTENING)

    asyncio.run(scenario())

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING"
    ]
    assert any(
        "aucune confirmation" in message.lower() for message in warnings
    )


def test_pending_playback_suspends_detection() -> None:
    async def scenario() -> None:
        cfg = AppConfig(wake_word=WakeWordConfig(cooldown_seconds=0.0))
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector({b"trigger": Detection(score=0.9)})
        app = make_app(cfg=cfg, transport=transport, detector=detector)

        # Audio serveur encore en file : le détecteur ne doit rien recevoir,
        # même si le Client est déjà revenu en écoute.
        await app.on_audio(b"pending-playback")
        await app.forward_audio(b"silence")
        assert detector.processed == []

        # La fin de Session vide la file ; la Veille est réarmée.
        await app.on_session_ended(SessionEnded())
        await app.forward_audio(b"silence")
        assert detector.processed == [b"silence"]

    asyncio.run(scenario())


def test_session_ended_flushes_playback_and_blocks_detection_during_cooldown() -> None:
    async def scenario() -> None:
        now = [100.0]
        cfg = AppConfig(wake_word=WakeWordConfig(cooldown_seconds=2.0))
        transport = FakeTransport()
        transport.connected = True
        detector = FakeDetector({b"trigger": Detection(score=0.9)})
        app = make_app(
            cfg=cfg,
            transport=transport,
            detector=detector,
            clock=lambda: now[0],
        )
        await app.on_session_started(SessionStarted())
        await app.on_audio(b"server-audio")
        assert not app._playback.empty()

        await app.on_session_ended(SessionEnded())

        assert app.state is ClientState.LISTENING
        assert app._playback.empty()

        # Pendant le cooldown, l'audio ne va pas au détecteur.
        await app.forward_audio(b"trigger")
        assert detector.processed == []

        now[0] += 2.001
        await app.forward_audio(b"trigger")
        assert detector.processed == [b"trigger"]
        assert transport.session_starts == 1

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
