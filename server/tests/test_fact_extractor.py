from __future__ import annotations

import asyncio

from conftest import RecordingTraceSink
from server.adapters.memory import GeminiFactExtractor


def _extractor(generate, **kwargs) -> GeminiFactExtractor:
    return GeminiFactExtractor(
        api_key="test-key",
        model="gemini-3.1-flash-lite",
        generate=generate,
        **kwargs,
    )


def _run(extractor: GeminiFactExtractor, transcript: str) -> list[str]:
    return asyncio.run(extractor.extract(transcript))


def test_parses_a_json_array_of_facts() -> None:
    extractor = _extractor(lambda model, prompt: '["aime le café", "habite à Lyon"]')
    assert _run(extractor, "[user] bonjour") == ["aime le café", "habite à Lyon"]


def test_passes_the_configured_model_and_transcript_to_the_generator() -> None:
    calls: list[tuple[str, str]] = []

    def generate(model: str, prompt: str) -> str:
        calls.append((model, prompt))
        return "[]"

    _run(_extractor(generate), "[user] j'habite à Lyon")

    model, prompt = calls[0]
    assert model == "gemini-3.1-flash-lite"
    assert "j'habite à Lyon" in prompt


def test_strips_markdown_code_fences() -> None:
    extractor = _extractor(lambda model, prompt: '```json\n["un fait"]\n```')
    assert _run(extractor, "x") == ["un fait"]


def test_filters_non_string_and_blank_entries() -> None:
    extractor = _extractor(lambda model, prompt: '["ok", 3, "   ", null, "deux"]')
    assert _run(extractor, "x") == ["ok", "deux"]


def test_non_json_response_yields_no_facts() -> None:
    extractor = _extractor(lambda model, prompt: "désolé je ne peux pas")
    assert _run(extractor, "x") == []


def test_a_non_list_json_response_yields_no_facts() -> None:
    extractor = _extractor(lambda model, prompt: '{"fact": "x"}')
    assert _run(extractor, "x") == []


def test_a_failing_generator_never_raises_and_returns_empty() -> None:
    def generate(model: str, prompt: str) -> str:
        raise RuntimeError("boom")

    extractor = _extractor(generate)
    assert _run(extractor, "x") == []


def test_an_empty_transcript_skips_the_generator() -> None:
    calls: list[str] = []

    def generate(model: str, prompt: str) -> str:
        calls.append(prompt)
        return "[]"

    assert _run(_extractor(generate), "   ") == []
    assert calls == []


def test_extraction_is_traced_at_debug() -> None:
    sink = RecordingTraceSink()
    extractor = _extractor(lambda model, prompt: '["un fait"]', trace_sink=sink)

    _run(extractor, "x")

    assert any(entry.level == "debug" for entry in sink.entries)
