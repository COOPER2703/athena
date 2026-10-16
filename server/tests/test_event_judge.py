from __future__ import annotations

import asyncio

from conftest import RecordingTraceSink
from server.adapters.gemini import GeminiEventJudge
from server.core.ports import Judgment

PROMPT = "JUGE LES FAITS"


def _judge(generate, **kwargs) -> GeminiEventJudge:
    return GeminiEventJudge(
        api_key="test-key",
        model="gemini-3.8-flash",
        prompt=PROMPT,
        generate=generate,
        **kwargs,
    )


def _run(judge: GeminiEventJudge, facts: list[str]) -> Judgment:
    return asyncio.run(judge.judge(facts))


def test_a_true_verdict_carries_the_message_to_speak() -> None:
    judge = _judge(lambda model, prompt: '{"announce": true, "message": "Porte ouverte"}')
    assert _run(judge, ["[matrix] porte"]) == Judgment(
        announce=True, message="Porte ouverte"
    )


def test_a_false_verdict_is_silent() -> None:
    judge = _judge(lambda model, prompt: '{"announce": false, "message": ""}')
    assert _run(judge, ["[maison] 19 degrés"]) == Judgment(announce=False)


def test_strips_markdown_code_fences() -> None:
    judge = _judge(
        lambda model, prompt: '```json\n{"announce": true, "message": "ok"}\n```'
    )
    assert _run(judge, ["[m] x"]).announce is True


def test_passes_the_configured_model_prompt_and_tagged_facts_to_the_generator() -> None:
    calls: list[tuple[str, str]] = []

    def generate(model: str, prompt: str) -> str:
        calls.append((model, prompt))
        return '{"announce": false, "message": ""}'

    _run(_judge(generate), ["[matrix] porte", "[door] colis"])

    model, prompt = calls[0]
    assert model == "gemini-3.8-flash"
    assert PROMPT in prompt
    assert "[matrix] porte" in prompt
    assert "[door] colis" in prompt


def test_no_facts_never_calls_the_model_and_stays_silent() -> None:
    calls: list[object] = []

    def generate(model: str, prompt: str) -> str:
        calls.append((model, prompt))
        return '{"announce": true, "message": "x"}'

    assert _run(_judge(generate), []) == Judgment(announce=False)
    assert calls == []


def test_a_non_json_response_is_silent() -> None:
    judge = _judge(lambda model, prompt: "je ne sais pas")
    assert _run(judge, ["[m] x"]) == Judgment(announce=False)


def test_a_malformed_verdict_is_silent() -> None:
    judge = _judge(lambda model, prompt: '{"announce": "oui"}')
    assert _run(judge, ["[m] x"]) == Judgment(announce=False)


def test_a_generator_failure_is_silent_and_traced_not_raised() -> None:
    sink = RecordingTraceSink()

    def generate(model: str, prompt: str) -> str:
        raise RuntimeError("réseau")

    judge = _judge(generate, trace_sink=sink)

    assert _run(judge, ["[m] x"]) == Judgment(announce=False)
    assert sink.of_kind("judgment_failed")


def test_the_verdict_is_traced() -> None:
    sink = RecordingTraceSink()
    judge = _judge(
        lambda model, prompt: '{"announce": true, "message": "x"}', trace_sink=sink
    )

    _run(judge, ["[m] x"])

    entry = sink.of_kind("judgment_done")[-1]
    assert entry.payload == {"facts": 1, "announce": True}
