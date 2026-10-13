from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import websockets

from conftest import RecordingTraceSink, _url, register, wait_until
from protocol import Audio, SessionStart, SessionStarted, decode, encode
from server.adapters.gemini import GeminiLiveAdapter
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.root import App


class _FakeSession:
    async def send_realtime_input(self, **kwargs: Any) -> None:
        pass

    async def receive(self) -> Any:
        await asyncio.Event().wait()
        yield None  # pragma: no cover - never reached


class _FakeContext:
    async def __aenter__(self) -> _FakeSession:
        return _FakeSession()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeLiveConnect:
    def __call__(self, model: str, config: Any) -> _FakeContext:
        return _FakeContext()


class _TalkingSession:
    """Fake Live session: one audio turn makes the Session visible, then blocks."""

    def __init__(self, audio: bytes) -> None:
        self._audio = audio

    async def send_realtime_input(self, **kwargs: Any) -> None:
        pass

    async def receive(self) -> Any:
        yield SimpleNamespace(data=self._audio)
        await asyncio.Event().wait()
        yield None  # pragma: no cover - never reached


class _TalkingContext:
    def __init__(self, audio: bytes) -> None:
        self._audio = audio

    async def __aenter__(self) -> _TalkingSession:
        return _TalkingSession(self._audio)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _TalkingLiveConnect:
    """Fait parler chaque Connexion Live ouverte, pour rendre les Sessions visibles."""

    def __init__(self, audio: bytes = b"reply") -> None:
        self._audio = audio

    def __call__(self, model: str, config: Any) -> _TalkingContext:
        return _TalkingContext(self._audio)


def _session_visible(sink: RecordingTraceSink) -> bool:
    return any(
        entry.source == "core"
        and entry.kind == "LlmAudio"
        and entry.payload.get("to_state") == "SessionVisible"
        for entry in sink.entries
    )


def test_timeline_covers_connections_sessions_and_gemini_calls() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_FakeLiveConnect(),
            trace_sink=sink,
        )
        clock = AsyncioClock()
        app = App(
            transport=transport, gemini=gemini, clock=clock, trace_sink=sink
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            async with websockets.connect(_url(transport)) as ws:
                await register(ws, transport, client_name="desktop", drain=False)
                await ws.send(encode(SessionStart()))

                await wait_until(
                    lambda: any(
                        entry.source == "transport"
                        and entry.kind == "client_registered"
                        for entry in sink.entries
                    )
                )
                await wait_until(
                    lambda: any(
                        entry.source == "core"
                        and entry.kind == "SessionRequested"
                        for entry in sink.entries
                    )
                )
                await wait_until(
                    lambda: any(
                        entry.source == "gemini" and entry.kind == "connected"
                        for entry in sink.entries
                    )
                )

                from_state = sink.of_kind("SessionRequested")[-1].payload["from_state"]
                to_state = sink.of_kind("SessionRequested")[-1].payload["to_state"]
                assert from_state == "Idle"
                assert to_state == "LiveConnecting"
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        assert sink.of_kind("stopped")

    asyncio.run(scenario())


def test_stale_terminal_after_replacement_does_not_kill_the_new_session() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_TalkingLiveConnect(audio=b"reply"),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            async with websockets.connect(_url(transport)) as ws_a:
                await register(ws_a, transport, client_name="a", drain=False)
                await ws_a.send(encode(SessionStart()))
                assert decode(await asyncio.wait_for(ws_a.recv(), 1.0)) == (
                    SessionStarted(auto=False)
                )
                await wait_until(lambda: _session_visible(sink))

                # B taps while A's Session is still visible: the replacement
                # closes A's Live connection, depositing a stale LlmClosed(s1).
                async with websockets.connect(_url(transport)) as ws_b:
                    await register(ws_b, transport, client_name="b", drain=False)
                    await ws_b.send(encode(SessionStart()))

                    assert decode(await asyncio.wait_for(ws_b.recv(), 1.0)) == (
                        SessionStarted(auto=False)
                    )
                    assert decode(await asyncio.wait_for(ws_b.recv(), 1.0)) == Audio(
                        data=b"reply"
                    )
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        # A was replaced, not client-gone.
        reasons = [entry.payload["reason"] for entry in sink.of_kind("SessionClosed")]
        assert "REPLACED" in reasons

        # The stale terminal was traced and ignored: B's Session (s2) survived.
        stale = [
            entry
            for entry in sink.of_kind("LlmClosed")
            if entry.payload.get("received_connection_id") == "s1"
        ]
        assert stale, "the stale terminal event was not traced"
        assert stale[-1].level == "debug"
        assert stale[-1].payload["connection_id"] == "s2"

    asyncio.run(scenario())


def test_active_client_disconnect_ends_the_session_with_client_gone() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_TalkingLiveConnect(audio=b"reply"),
            trace_sink=sink,
        )
        app = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            async with websockets.connect(_url(transport)) as ws_a:
                await register(ws_a, transport, client_name="a", drain=False)
                await ws_a.send(encode(SessionStart()))
                assert decode(await asyncio.wait_for(ws_a.recv(), 1.0)) == (
                    SessionStarted(auto=False)
                )
                await wait_until(lambda: _session_visible(sink))

            # A's socket closed: the core must end the Session, not leave a ghost.
            await wait_until(
                lambda: any(
                    entry.kind == "SessionClosed"
                    and entry.payload.get("reason") == "CLIENT_GONE"
                    for entry in sink.entries
                )
            )
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        closed = sink.of_kind("SessionClosed")
        assert closed[-1].payload["reason"] == "CLIENT_GONE"
        assert closed[-1].payload["to_state"] == "Idle"

    asyncio.run(scenario())
