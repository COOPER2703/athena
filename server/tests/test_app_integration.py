from __future__ import annotations

import asyncio
from typing import Any

import websockets

from conftest import RecordingTraceSink, _url, register, wait_until
from protocol import SessionStart, encode
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
