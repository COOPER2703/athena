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
    SendMemoryToLlm,
    SendToolResultToLlm,
    StartIdleTimer,
    Trace,
)
from server.core.events import (
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
    LlmToolCall,
    LlmTranscription,
    Role,
)
from server.core.ports import ToolSpec


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
        self.sent_client_content: list[Any] = []
        self.sent_tool_responses: list[Any] = []

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.sent_realtime.append(kwargs)

    async def send_client_content(self, **kwargs: Any) -> None:
        self.sent_client_content.append(kwargs)

    async def send_tool_response(self, **kwargs: Any) -> None:
        self.sent_tool_responses.append(kwargs)

    async def receive(self) -> Any:
        if self._block:
            await asyncio.Event().wait()
        while self._responses:
            yield self._responses.pop(0)
        if self._receive_error is not None:
            raise self._receive_error


class PerTurnSession:
    """Models google-genai's per-turn ``receive()`` contract.

    ``AsyncSession.receive()`` yields one complete model turn and returns while
    the connection stays open; a later call yields the next turn. This fake
    reproduces that seam, unlike ``FakeSession`` which drains a single iterator.
    """

    def __init__(self, turns: list[list[Any]]) -> None:
        self._turns = list(turns)
        self.sent_realtime: list[Any] = []
        self.sent_client_content: list[Any] = []
        self.sent_tool_responses: list[Any] = []

    async def send_realtime_input(self, **kwargs: Any) -> None:
        self.sent_realtime.append(kwargs)

    async def send_client_content(self, **kwargs: Any) -> None:
        self.sent_client_content.append(kwargs)

    async def send_tool_response(self, **kwargs: Any) -> None:
        self.sent_tool_responses.append(kwargs)

    async def receive(self) -> Any:
        if not self._turns:
            await asyncio.Event().wait()
        turn = self._turns.pop(0)
        for response in turn:
            yield response


class FakeLiveConnect:
    def __init__(
        self,
        session: FakeSession | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.session = session
        self.error = error
        self.calls: list[tuple[str, Any]] = []
        self.contexts: list[FakeContext] = []
        self.entered = False
        self.exited = False

    def __call__(self, model: str, config: Any) -> "FakeContext":
        self.calls.append((model, config))
        context = FakeContext(self)
        self.contexts.append(context)
        return context


class FakeContext:
    def __init__(self, live_connect: FakeLiveConnect) -> None:
        self._live_connect = live_connect
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> FakeSession:
        if self._live_connect.error is not None:
            raise self._live_connect.error
        self.entered = True
        self._live_connect.entered = True
        assert self._live_connect.session is not None
        return self._live_connect.session

    async def __aexit__(self, *exc: Any) -> bool:
        self.exited = True
        self._live_connect.exited = True
        return False


def _adapter(
    live_connect: FakeLiveConnect | None = None,
    *,
    model: str = "gemini-live",
    voice: str = "Aoede",
) -> GeminiLiveAdapter:
    return GeminiLiveAdapter(
        api_key="test-key",
        model=model,
        voice=voice,
        live_connect=live_connect or FakeLiveConnect(session=FakeSession()),
    )


def test_open_emits_opened_and_hands_config_to_the_live_connect() -> None:
    async def scenario() -> None:
        session = FakeSession()
        live_connect = FakeLiveConnect(session=session)
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))

        event = await asyncio.wait_for(adapter.recv(), 1.0)
        assert isinstance(event, LlmOpened)

        model, config = live_connect.calls[0]
        assert model == "gemini-live"
        assert config.response_modalities == ["AUDIO"]
        voice_name = config.speech_config.voice_config.prebuilt_voice_config.voice_name
        assert voice_name == "Aoede"
        assert config.input_audio_transcription is not None
        assert config.output_audio_transcription is not None

    asyncio.run(scenario())


def test_open_forwards_the_system_instruction_to_the_live_config() -> None:
    async def scenario() -> None:
        live_connect = FakeLiveConnect(session=FakeSession())
        adapter = _adapter(live_connect)

        await adapter.open("s1", "RÈGLES DU NOYAU")

        _, config = live_connect.calls[0]
        assert config.system_instruction.parts[0].text == "RÈGLES DU NOYAU"

    asyncio.run(scenario())


def test_open_without_instruction_leaves_the_system_instruction_empty() -> None:
    async def scenario() -> None:
        live_connect = FakeLiveConnect(session=FakeSession())
        adapter = _adapter(live_connect)

        await adapter.open("s1")

        _, config = live_connect.calls[0]
        assert config.system_instruction is None

    asyncio.run(scenario())


def test_open_puts_tool_declarations_into_the_live_config() -> None:
    async def scenario() -> None:
        live_connect = FakeLiveConnect(session=FakeSession(block=True))
        adapter = _adapter(live_connect)
        spec = ToolSpec(
            name="save_memory",
            description="mémorise un fait",
            parameters={
                "type": "object",
                "properties": {"content": {"type": "string"}},
            },
        )

        await adapter.open("s1", tools=(spec,))

        _, config = live_connect.calls[0]
        declarations = config.tools[0].function_declarations
        assert declarations[0].name == "save_memory"
        assert declarations[0].description == "mémorise un fait"
        assert declarations[0].parameters_json_schema == {
            "type": "object",
            "properties": {"content": {"type": "string"}},
        }

    asyncio.run(scenario())


def test_open_without_tools_omits_the_declaration_list() -> None:
    async def scenario() -> None:
        live_connect = FakeLiveConnect(session=FakeSession())
        adapter = _adapter(live_connect)

        await adapter.open("s1")

        _, config = live_connect.calls[0]
        assert not config.tools

    asyncio.run(scenario())


def test_tool_call_response_is_translated_into_an_llm_tool_call() -> None:
    async def scenario() -> None:
        function_call = SimpleNamespace(
            id="1", name="save_memory", args={"content": "x", "kind": "profile"}
        )
        responses = [
            SimpleNamespace(tool_call=SimpleNamespace(function_calls=[function_call]))
        ]
        live_connect = FakeLiveConnect(session=FakeSession(responses=responses))
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmToolCall(
            tool_id="1",
            name="save_memory",
            arguments={"content": "x", "kind": "profile"},
        )

    asyncio.run(scenario())


def test_send_tool_result_forwards_a_function_response_with_the_echoed_id() -> None:
    async def scenario() -> None:
        session = FakeSession(block=True)
        live_connect = FakeLiveConnect(session=session)
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        await adapter.send_tool_result("s1", "1", "save_memory", {"status": "ok"})

        assert len(session.sent_tool_responses) == 1
        response = session.sent_tool_responses[0]["function_responses"]
        assert response.id == "1"
        assert response.name == "save_memory"
        assert response.response == {"status": "ok"}

        await adapter.execute(CloseLiveConnection(connection_id="s1"))

    asyncio.run(scenario())


def test_execute_routes_send_tool_result_through_the_uniform_seam() -> None:
    async def scenario() -> None:
        session = FakeSession(block=True)
        live_connect = FakeLiveConnect(session=session)
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        await adapter.execute(
            SendToolResultToLlm(
                connection_id="s1",
                tool_id="1",
                name="save_memory",
                result={"status": "ok"},
            )
        )

        assert len(session.sent_tool_responses) == 1
        response = session.sent_tool_responses[0]["function_responses"]
        assert response.id == "1"
        assert response.name == "save_memory"
        assert response.response == {"status": "ok"}

        await adapter.execute(CloseLiveConnection(connection_id="s1"))

    asyncio.run(scenario())


def test_send_tool_result_without_open_connection_is_ignored() -> None:
    async def scenario() -> None:
        live_connect = FakeLiveConnect(session=FakeSession())
        adapter = _adapter(live_connect)

        await adapter.send_tool_result("s1", "1", "echo", {})

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_maps_audio_transcriptions_and_traces_tool_calls() -> None:
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
        live_connect = FakeLiveConnect(session=FakeSession(responses=responses))
        sink = RecordingTraceSink()
        adapter = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=live_connect,
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
        assert sink.of_kind("tool_call")

    asyncio.run(scenario())


def test_audio_and_transcription_in_one_message_are_both_emitted() -> None:
    async def scenario() -> None:
        response = SimpleNamespace(
            data=b"\x10\x20",
            server_content=SimpleNamespace(
                input_transcription=SimpleNamespace(text="bonjour"),
                output_transcription=SimpleNamespace(text="salut"),
            ),
        )
        live_connect = FakeLiveConnect(session=FakeSession(responses=[response]))
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmAudio(data=b"\x10\x20")
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmTranscription(
            text="bonjour", role=Role.USER
        )
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmTranscription(
            text="salut", role=Role.ASSISTANT
        )
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmClosed()

    asyncio.run(scenario())


def test_downstream_pcm16_mono_24khz_audio_is_passed_through_chunk_by_chunk() -> None:
    async def scenario() -> None:
        chunks = [b"\x00\x01" * 480, b"\x02" * 960, b"\x03" * 240]
        responses = [SimpleNamespace(data=chunk) for chunk in chunks]
        live_connect = FakeLiveConnect(session=FakeSession(responses=responses))
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        for chunk in chunks:
            assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmAudio(data=chunk)

        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmClosed()

    asyncio.run(scenario())


def test_open_failure_reports_error_without_raising() -> None:
    async def scenario() -> None:
        live_connect = FakeLiveConnect(error=RuntimeError("refused"))
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))

        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmFailed(
            cause=LlmFailureCause.ERROR
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_open_tears_down_a_previous_live_connection() -> None:
    async def scenario() -> None:
        live_connect = FakeLiveConnect(session=FakeSession(block=True))
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        await adapter.execute(OpenLiveConnection(connection_id="s2"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        assert len(live_connect.contexts) == 2
        assert live_connect.contexts[0].exited
        assert not live_connect.contexts[1].exited

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

        await adapter.execute(CloseLiveConnection(connection_id="s2"))
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmClosed()

    asyncio.run(scenario())


def test_go_away_reports_failure_once() -> None:
    async def scenario() -> None:
        responses = [SimpleNamespace(go_away=SimpleNamespace(time_left="5s"))]
        live_connect = FakeLiveConnect(session=FakeSession(responses=responses))
        adapter = _adapter(live_connect)

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
        live_connect = FakeLiveConnect(session=FakeSession(block=True))
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        await adapter.execute(CloseLiveConnection(connection_id="s1"))
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmClosed()
        assert live_connect.exited

        await adapter.execute(CloseLiveConnection(connection_id="s1"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_consecutive_turns_do_not_close_the_live_connection() -> None:
    async def scenario() -> None:
        turns = [
            [SimpleNamespace(data=b"\x11")],
            [SimpleNamespace(data=b"\x22")],
        ]
        live_connect = FakeLiveConnect(session=PerTurnSession(turns))
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmAudio(data=b"\x11")
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmAudio(data=b"\x22")

        await adapter.execute(CloseLiveConnection(connection_id="s1"))
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmClosed()

    asyncio.run(scenario())


def test_end_of_stream_emits_closed() -> None:
    async def scenario() -> None:
        live_connect = FakeLiveConnect(session=FakeSession())
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmClosed()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_send_audio_forwards_exact_bytes_as_pcm16_blob() -> None:
    async def scenario() -> None:
        session = FakeSession(block=True)
        live_connect = FakeLiveConnect(session=session)
        adapter = _adapter(live_connect)

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
        live_connect = FakeLiveConnect(session=FakeSession())
        adapter = _adapter(live_connect)

        await adapter.execute(SendAudioToLlm(connection_id="s1", data=b"\x01"))

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_send_audio_after_terminal_event_is_ignored() -> None:
    async def scenario() -> None:
        responses = [SimpleNamespace(go_away=SimpleNamespace(time_left="1s"))]
        session = FakeSession(responses=responses)
        live_connect = FakeLiveConnect(session=session)
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmFailed(
            cause=LlmFailureCause.GO_AWAY
        )

        await adapter.execute(SendAudioToLlm(connection_id="s1", data=b"\xaa\xbb"))

        assert session.sent_realtime == []

    asyncio.run(scenario())


def test_send_memory_injects_the_memoire_block_without_completing_the_turn() -> None:
    async def scenario() -> None:
        session = FakeSession(block=True)
        live_connect = FakeLiveConnect(session=session)
        sink = RecordingTraceSink()
        adapter = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=live_connect,
            trace_sink=sink,
        )

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)

        await adapter.execute(
            SendMemoryToLlm(connection_id="s1", text="[MÉMOIRE]\n- bleu")
        )

        assert len(session.sent_client_content) == 1
        call = session.sent_client_content[0]
        assert call["turns"].role == "user"
        assert call["turns"].parts[0].text == "[MÉMOIRE]\n- bleu"
        assert call["turn_complete"] is False
        assert sink.of_kind("memory_injected")

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_send_memory_without_open_connection_is_ignored() -> None:
    async def scenario() -> None:
        live_connect = FakeLiveConnect(session=FakeSession())
        sink = RecordingTraceSink()
        adapter = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=live_connect,
            trace_sink=sink,
        )

        await adapter.execute(SendMemoryToLlm(connection_id="s1", text="x"))

        assert sink.of_kind("send_memory_ignored")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.recv(), 0.05)

    asyncio.run(scenario())


def test_send_memory_after_terminal_event_is_ignored() -> None:
    async def scenario() -> None:
        responses = [SimpleNamespace(go_away=SimpleNamespace(time_left="1s"))]
        session = FakeSession(responses=responses)
        live_connect = FakeLiveConnect(session=session)
        adapter = _adapter(live_connect)

        await adapter.execute(OpenLiveConnection(connection_id="s1"))
        assert isinstance(await asyncio.wait_for(adapter.recv(), 1.0), LlmOpened)
        assert await asyncio.wait_for(adapter.recv(), 1.0) == LlmFailed(
            cause=LlmFailureCause.GO_AWAY
        )

        await adapter.execute(SendMemoryToLlm(connection_id="s1", text="x"))

        assert session.sent_client_content == []

    asyncio.run(scenario())


def test_receive_exception_reports_error_failure() -> None:
    async def scenario() -> None:
        session = FakeSession(receive_error=RuntimeError("boom"))
        live_connect = FakeLiveConnect(session=session)
        adapter = _adapter(live_connect)

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
        live_connect = FakeLiveConnect(session=FakeSession(block=True))
        adapter = GeminiLiveAdapter(
            api_key="test-key",
            model="gemini-live",
            voice="Aoede",
            live_connect=live_connect,
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
