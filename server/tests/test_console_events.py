from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import aiohttp
from conftest import FakeJudge, RecordingTraceSink, wait_until

from server.adapters.console import EVENTS_PATH, ConsoleAdapter
from server.adapters.gemini import GeminiLiveAdapter
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.root import App


class _Recorder:
    """Couture ``push`` de test : enregistre les Événements reçus."""

    def __init__(self) -> None:
        self.pushes: list[tuple[str, str]] = []

    def __call__(self, source: str, content: str) -> None:
        self.pushes.append((source, content))


def _snapshot() -> dict[str, object]:
    return {
        "session": {
            "state": "Idle",
            "session_id": "",
            "connection_id": "",
            "active_client_id": "",
        },
        "clients": [],
        "plugins": [],
        "config": {},
    }


def _url(adapter: ConsoleAdapter, path: str = EVENTS_PATH) -> str:
    return f"http://127.0.0.1:{adapter.port}{path}"


def test_console_event_is_accepted_pushed_and_traced() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        sink = RecordingTraceSink()
        adapter = ConsoleAdapter(port=0, trace_sink=sink)
        await adapter.start(_snapshot, recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    json={"source": "matrix", "content": "porte ouverte"},
                ) as response:
                    assert response.status == 202
                    assert await response.json() == {"status": "accepted"}
            assert recorder.pushes == [("matrix", "porte ouverte")]
            [entry] = sink.of_kind("event_received")
            assert entry.source == "console"
            assert entry.payload == {"source": "matrix", "chars": 13}
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_missing_source_defaults_to_console() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        sink = RecordingTraceSink()
        adapter = ConsoleAdapter(port=0, trace_sink=sink)
        await adapter.start(_snapshot, recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter), json={"content": "porte ouverte"}
                ) as response:
                    assert response.status == 202
            assert recorder.pushes == [("console", "porte ouverte")]
            [entry] = sink.of_kind("event_received")
            assert entry.payload == {"source": "console", "chars": 13}
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_non_json_content_type_is_rejected() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = ConsoleAdapter(port=0)
        await adapter.start(_snapshot, recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    data='{"content": "porte ouverte"}',
                    headers={"Content-Type": "text/plain"},
                ) as response:
                    assert response.status == 415
                    assert await response.json() == {
                        "error": "Content-Type must be application/json"
                    }
            assert recorder.pushes == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_invalid_json_body_is_rejected() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = ConsoleAdapter(port=0)
        await adapter.start(_snapshot, recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    data="{not json",
                    headers={"Content-Type": "application/json"},
                ) as response:
                    assert response.status == 400
                    assert await response.json() == {"error": "invalid JSON body"}
            assert recorder.pushes == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_invalid_body_or_fields_are_rejected() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = ConsoleAdapter(port=0)
        await adapter.start(_snapshot, recorder)
        try:
            async with aiohttp.ClientSession() as session:
                for payload in (
                    ["not", "an", "object"],
                    {"content": 12},
                    {"source": "  ", "content": "porte ouverte"},
                    {"content": "   "},
                ):
                    async with session.post(
                        _url(adapter), json=payload
                    ) as response:
                        assert response.status == 400
                        body = await response.json()
                        assert body["error"]
            assert recorder.pushes == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_oversized_content_is_rejected_without_pushing() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        sink = RecordingTraceSink()
        adapter = ConsoleAdapter(port=0, trace_sink=sink)
        await adapter.start(_snapshot, recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    json={"content": "x" * 4097},
                ) as response:
                    assert response.status == 413
                    body = await response.json()
                    assert body["error"]
            assert recorder.pushes == []
            assert sink.of_kind("event_received") == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_without_a_push_seam_the_injection_is_not_serving() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        adapter = ConsoleAdapter(port=0, trace_sink=sink)
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter), json={"content": "porte ouverte"}
                ) as response:
                    assert response.status == 503
                    assert await response.json() == {
                        "error": "console not serving"
                    }
            assert sink.of_kind("event_received") == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


class _BlockingSession:
    async def receive(self) -> Any:
        await asyncio.Event().wait()
        yield None  # pragma: no cover - jamais atteint


class _BlockingContext:
    async def __aenter__(self) -> _BlockingSession:
        return _BlockingSession()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _BlockingLiveConnect:
    def __call__(self, model: str, config: Any) -> _BlockingContext:
        return _BlockingContext()


class _FastClock(AsyncioClock):
    """Horloge de test : ramène les échéances à une fraction de seconde.

    Le câblage reste réel ; seule l'attente de la fenêtre de regroupement
    d'Événements (5 s en production) est raccourcie pour que le test reste vif.
    """

    def schedule(self, connection_id: str, timeout: float) -> None:
        super().schedule(connection_id, min(timeout, 0.05))


def test_an_injected_console_event_enters_the_channel_and_is_judged() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=_BlockingLiveConnect(),
            trace_sink=sink,
        )
        judge = FakeJudge(announce=False)
        console = ConsoleAdapter(port=0, trace_sink=sink)
        app = App(
            transport=transport,
            gemini=gemini,
            clock=_FastClock(),
            trace_sink=sink,
            console=console,
            judge=judge,
        )
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"http://127.0.0.1:{console.port}{EVENTS_PATH}",
                    json={"content": "porte ouverte"},
                ) as response:
                    assert response.status == 202
                    assert await response.json() == {"status": "accepted"}

            # L'Événement entre dans le Canal unique, ouvre la fenêtre, puis est
            # jugé par le noyau avec sa Source par défaut.
            await wait_until(lambda: bool(judge.calls))
            await wait_until(lambda: bool(sink.of_kind("EventJudged")))
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

        [received] = sink.of_kind("event_received")
        assert received.source == "console"
        assert received.payload == {"source": "console", "chars": 13}
        assert judge.calls == [["[console] porte ouverte"]]
        judged = sink.of_kind("EventJudged")[-1]
        assert judged.payload["events"] == 1
        assert judged.payload["announce"] is False

    asyncio.run(scenario())
