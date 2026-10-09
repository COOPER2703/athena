from __future__ import annotations

from types import SimpleNamespace

from server.adapters.gemini.translation import translate_response
from server.core.events import (
    LlmAudio,
    LlmFailed,
    LlmFailureCause,
    LlmTranscription,
    Role,
)


def test_audio_data_maps_to_llm_audio() -> None:
    assert translate_response(SimpleNamespace(data=b"pcm")) == [LlmAudio(data=b"pcm")]


def test_input_transcription_maps_to_user_transcription() -> None:
    response = SimpleNamespace(
        server_content=SimpleNamespace(
            input_transcription=SimpleNamespace(text="bonjour"),
            output_transcription=None,
        )
    )
    assert translate_response(response) == [
        LlmTranscription(text="bonjour", role=Role.USER)
    ]


def test_output_transcription_maps_to_assistant_transcription() -> None:
    response = SimpleNamespace(
        server_content=SimpleNamespace(
            input_transcription=None,
            output_transcription=SimpleNamespace(text="salut"),
        )
    )
    assert translate_response(response) == [
        LlmTranscription(text="salut", role=Role.ASSISTANT)
    ]


def test_audio_and_transcription_in_one_message_yield_both_events() -> None:
    response = SimpleNamespace(
        data=b"\x10\x20",
        server_content=SimpleNamespace(
            input_transcription=SimpleNamespace(text="bonjour"),
            output_transcription=SimpleNamespace(text="salut"),
        ),
    )
    assert translate_response(response) == [
        LlmAudio(data=b"\x10\x20"),
        LlmTranscription(text="bonjour", role=Role.USER),
        LlmTranscription(text="salut", role=Role.ASSISTANT),
    ]


def test_go_away_maps_to_go_away_failure() -> None:
    response = SimpleNamespace(go_away=SimpleNamespace(time_left="5s"))
    assert translate_response(response) == [LlmFailed(cause=LlmFailureCause.GO_AWAY)]


def test_tool_call_yields_no_events() -> None:
    response = SimpleNamespace(tool_call=SimpleNamespace(function_calls=[]))
    assert translate_response(response) == []


def test_message_without_known_fields_yields_no_events() -> None:
    assert translate_response(SimpleNamespace()) == []
