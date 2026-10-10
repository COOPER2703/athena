from __future__ import annotations

import asyncio
from typing import Any

from client.app import ClientApp
from client.config import AppConfig, ClientConfig
from client.ws_client import WsTransport
from conftest import RecordingTraceSink, _url, wait_until
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.root import App
from server.core.commands import OpenLiveConnection, SendAudioToLlm
from server.core.events import LlmAudio, LlmOpened


class FakeGemini:
    """Stands in for Gemini Live: echoes an audio reply once it hears the user."""

    def __init__(self) -> None:
        self._events: asyncio.Queue[Any] = asyncio.Queue()
        self.received: list[bytes] = []

    async def recv(self) -> Any:
        return await self._events.get()

    async def execute(self, command: Any) -> None:
        if isinstance(command, OpenLiveConnection):
            await self._events.put(LlmOpened())
        elif isinstance(command, SendAudioToLlm):
            self.received.append(command.data)
            await self._events.put(LlmAudio(data=b"\x99\x99"))


class MicAudio:
    """Continuous fake mic: every chunk is voice."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._running = True
        self.flushed = 0

    async def start_input(self) -> None: ...

    async def start_output(self) -> None: ...

    async def read_chunk(self) -> bytes:
        await asyncio.sleep(0.005)
        return b"\x01\x02" if self._running else b""

    async def write_chunk(self, data: bytes) -> None:
        self.written.append(data)

    async def flush(self) -> None:
        self.flushed += 1

    async def close(self) -> None:
        self._running = False


def test_tap_then_speak_yields_audio_response() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
        gemini = FakeGemini()
        server = App(
            transport=transport,
            gemini=gemini,
            clock=AsyncioClock(),
            trace_sink=sink,
        )
        server_task = asyncio.create_task(server.run())
        client_task: asyncio.Task[None] | None = None
        try:
            await wait_until(lambda: bool(sink.of_kind("started")))

            audio = MicAudio()
            ws = WsTransport(ClientConfig(server_url=_url(transport)))
            client = ClientApp(AppConfig(), audio=audio, transport=ws)
            client_task = asyncio.create_task(client.run())
            try:
                await wait_until(lambda: ws.is_connected and ws.client_id is not None)

                await client.tap()
                await wait_until(lambda: bool(gemini.received), timeout=2.0)
                await wait_until(lambda: bool(audio.written), timeout=2.0)

                assert gemini.received, "mic audio never reached the LLM"
                assert audio.written, "no audio response after speaking"
            finally:
                client.stop()
                await asyncio.wait_for(client_task, 1.0)
                client_task = None
        finally:
            server.request_shutdown()
            await asyncio.wait_for(server_task, 1.0)

    asyncio.run(scenario())
