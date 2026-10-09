from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import RecordingTraceSink
from server.adapters.gemini import GeminiLiveAdapter
from server.core.commands import (
    CloseLiveConnection,
    OpenLiveConnection,
    SendAudioToLlm,
    StartIdleTimer,
    Trace,
)
from server.core.events import (
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
    LlmTranscription,
    Role,
)


class FakeSession:
    def __init__(
        self,
        responses: list[Any] | None = None,
        receive_error: BaseException | None = None,
        block: bool = False,
    ) -> None:
        self._responses = list(responses or [])
        self._receive_error = receive_error
        self._block = block
        self.sent_realtime: list[Any] = []

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.sent_realtime.append(kwargs)

    async def receive(self) -> Any:
        if self._block:
            await asyncio.Event().wait()
        for response in self._responses:
            yield response
        if self._receive_error is not None:
            raise self._receive_error


class FakeConnector:
    def __init__(
        self,
        session: FakeSession | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.session = session
        self.error = error
        self.calls: list[tuple[str, Any]] = []
        self.entered = False
        self.exited = False

    def __call__(self, model: str, config: Any) -> "FakeContext":
        self.calls.append((model, config))
        return FakeContext(self)


class FakeContext:
    def __init__(self, connector: FakeConnector) -> None:
        self._connector = connector

    async def __aenter__(self) -> FakeSession:
        if self._connector.error is not None:
            raise self._connector.error
        self._connector.entered = True
        assert self._connector.session is not None
        return self._connector.session

    async def __aexit__(self, *exc: Any) -> bool:
        self._connector.exited = True
        return False


def _adapter(
    connector: FakeConnector | None = None,
    *,
    model: str = "gemini-live",
    voice: str = "Aoede",
) -> GeminiLiveAdapter:
    return GeminiLiveAdapter(
        api_key="test-key",
        model=model,
        voice=voice,
        connector=connector or FakeConnector(session=FakeSession()),
    )


def test_open_emits_opened_and_hands_config_to_the_connector() -> None:
    async def scenario() -> None:
        session = FakeSession()
        connector = FakeConnector(session=session)
        adapter = _adapter(connector)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))

        event = await asyncio.wait_for(adapter.recv(), 1.0)
        assert isinstance(event, LlmOpened)

        model, config = connector.calls[0]
        assert model == "gemini-live"
        assert config.response_modalities == ["AUDIO"]
        voice_name = config.speech_config.voice_config.prebuilt_voice_config.voice_name
        assert voice_name == "Aoede"
        assert config.input_audio_transcription is not None
        assert config.output_audio_transcription is not None

    asyncio.run(scenario())


def test_maps_audio_transcriptions_and_ignores_tool_call() -> None:
    async def scenario() -> None:
        responses = [
            SimpleNamespace(data=b"\x10\x20"),
            SimpleNamespace(
                server_content=SimpleNamespace(
                    input_transcription=SimpleNamespace(text="bonjour"),
                    output_transcription=None,
                )
            ),
            SimpleNamespace(tool_call=SimpleNamespace(function_calls=[])),
            SimpleNamespace(
                server_content=SimpleNamespace(
                    input_transcription=None,
                    output_transcription=SimpleNamespace(text="salut"),
                )
            ),
        ]
        connector = FakeConnector(session=FakeSession(responses=responses))
        sink = RecordingTraceSink()
        adapter = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            connector=connector,
            trace_sink=sink,
        )

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmAudio(data=b"\x10\x20")
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmTranscription(
            text="bonjour", role=Role.USER
        )
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmTranscription(
            text="salut", role=Role.ASSISTANT
        )
        assert sink.of_kind("tool_call_ignored")

    asyncio.run(scenario())


def test_open_failure_reports_error_without_raising() -> None:
    async def scenario() -> None:
        connector = FakeConnector(error=RuntimeError("refused"))
        adapter = _adapter(connector)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))

        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmFailed(
            cause=LlmFailureCause.ERROR
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_go_away_reports_failure_once() -> None:
    async def scenario() -> None:
        responses = [SimpleNamespace(go_away=SimpleNamespace(time_left="5s"))]
        connector = FakeConnector(session=FakeSession(responses=responses))
        adapter = _adapter(connector)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmFailed(
            cause=LlmFailureCause.GO_AWAY
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_close_emits_closed_once_and_exits_the_context() -> None:
    async def scenario() -> None:
        connector = FakeConnector(session=FakeSession(block=True))
        adapter = _adapter(connector)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        await adapter.execute(CloseLiveConnection(connection_id="s1"))
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmClosed()
        assert connector.exited

        await adapter.execute(CloseLiveConnection(connection_id="s1"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_end_of_stream_emits_closed() -> None:
    async def scenario() -> None:
        connector = FakeConnector(session=FakeSession())
        adapter = _adapter(connector)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmClosed()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_send_audio_forwards_exact_bytes_as_pcm16_blob() -> None:
    async def scenario() -> None:
        session = FakeSession(block=True)
        connector = FakeConnector(session=session)
        adapter = _adapter(connector)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        await adapter.execute(SendAudioToLlm(connection_id="s1", data=b"\xaa\xbb"))

        assert len(session.sent_realtime) == 1
        blob = session.sent_realtime[0]["audio"]
        assert blob.data == b"\xaa\xbb"
        assert blob.mime_type == "audio/pcm;rate=16000"

        await adapter.execute(CloseLiveConnection(connection_id="s1"))

    asyncio.run(scenario())


def test_send_audio_without_open_connection_is_ignored() -> None:
    async def scenario() -> None:
        connector = FakeConnector(session=FakeSession())
        adapter = _adapter(connector)

        await adapter.execute(SendAudioToLlm(connection_id="s1", data=b"\x01"))

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_receive_exception_reports_error_failure() -> None:
    async def scenario() -> None:
        session = FakeSession(receive_error=RuntimeError("boom"))
        connector = FakeConnector(session=session)
        adapter = _adapter(connector)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmFailed(
            cause=LlmFailureCause.ERROR
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_connect_and_disconnect_are_traced_and_trace_command_forwarded() -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        connector = FakeConnector(session=FakeSession(block=True))
        adapter = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            connector=connector,
            trace_sink=sink,
        )

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)
        assert sink.of_kind("connected")

        await adapter.execute(CloseLiveConnection(connection_id="s1"))
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmClosed()
        assert sink.of_kind("disconnected")

        await adapter.execute(
            Trace(source="core", kind="X", payload={"a": 1}, level="debug")
        )
        entry = sink.entries[-1]
        assert entry.source == "core"
        assert entry.kind == "X"
        assert entry.payload == {"a": 1}
        assert entry.level == "debug"

    asyncio.run(scenario())


def test_unknown_command_is_ignored_without_crashing() -> None:
    async def scenario() -> None:
        adapter = _adapter()

        await adapter.execute(StartIdleTimer(connection_id="s1", timeout=1.0))

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())
