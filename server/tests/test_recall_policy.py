from __future__ import annotations

import asyncio

from server.core import (
    IDLE_TIMEOUT,
    Coordinator,
    LlmAudio,
    LlmClosed,
    LlmOpened,
    LlmTranscription,
    MemoryHit,
    Role,
    SendMemoryToLlm,
    SessionRequested,
    StartIdleTimer,
    Trace,
)


class FakeMemory:
    """Pure in-memory ``MemoryStore`` double (no I/O, no torch)."""

    def __init__(
        self, hits: list[MemoryHit] | None = None, context: str = ""
    ) -> None:
        self.context = context
        self._hits = list(hits or [])
        self.turns: list[tuple[str, str, str]] = []
        self.queries: list[tuple[str, str]] = []

    async def session_context(self, user_id: str) -> str:
        return self.context

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        self.turns.append((user_id, role, text))

    async def recall(self, user_id: str, query: str) -> list[MemoryHit]:
        self.queries.append((user_id, query))
        return list(self._hits)


def drive(coordinator: Coordinator, events) -> list:
    async def run():
        commands: list = []
        for event in events:
            commands.extend(await coordinator.handle(event))
        return commands

    return asyncio.run(run())


def effects(commands) -> list:
    return [command for command in commands if not isinstance(command, Trace)]


def memories(commands) -> list[SendMemoryToLlm]:
    return [command for command in commands if isinstance(command, SendMemoryToLlm)]


def open_session(coordinator: Coordinator, client_id: str = "c1") -> list:
    return drive(coordinator, [SessionRequested(client_id=client_id), LlmOpened()])


def say(coordinator: Coordinator, text: str) -> list:
    return asyncio.run(
        coordinator.handle(LlmTranscription(role=Role.USER, text=text))
    )


def test_query_is_the_prior_window_plus_the_current_utterance():
    memory = FakeMemory()
    coordinator = Coordinator(memory=memory, user_id="alice", recall_window_turns=3)
    open_session(coordinator)

    for text in ["a", "b", "c", "d", "e"]:
        say(coordinator, text)

    assert memory.queries[-1] == ("alice", "b c d e")


def test_user_utterance_is_journaled_then_recalled():
    memory = FakeMemory()
    coordinator = Coordinator(memory=memory, user_id="alice")
    open_session(coordinator)

    say(coordinator, "salut")

    assert memory.turns[-1] == ("alice", "user", "salut")
    assert memory.queries[-1] == ("alice", "salut")


def test_hits_are_injected_under_the_memoire_header():
    memory = FakeMemory(hits=[MemoryHit("bleu", 0.2, "journal")])
    coordinator = Coordinator(memory=memory)
    open_session(coordinator)

    commands = say(coordinator, "ma couleur")

    assert memories(commands) == [
        SendMemoryToLlm(connection_id="s1", text="[MÉMOIRE]\n- bleu")
    ]


def test_hits_beyond_the_distance_threshold_are_dropped():
    memory = FakeMemory(
        hits=[
            MemoryHit("proche", 0.2, "journal"),
            MemoryHit("loin", 0.9, "journal"),
        ]
    )
    coordinator = Coordinator(memory=memory, recall_max_distance=0.6)
    open_session(coordinator)

    commands = say(coordinator, "salut")

    assert memories(commands)[0].text == "[MÉMOIRE]\n- proche"


def test_empty_hits_are_dropped():
    memory = FakeMemory(
        hits=[
            MemoryHit("", 0.1, "journal"),
            MemoryHit("ok", 0.2, "journal"),
        ]
    )
    coordinator = Coordinator(memory=memory)
    open_session(coordinator)

    commands = say(coordinator, "salut")

    assert memories(commands)[0].text == "[MÉMOIRE]\n- ok"


def test_a_memory_is_injected_at_most_once_per_session():
    memory = FakeMemory(hits=[MemoryHit("bleu", 0.2, "journal")])
    coordinator = Coordinator(memory=memory)
    open_session(coordinator)

    assert memories(say(coordinator, "un"))
    assert memories(say(coordinator, "deux")) == []


def test_injection_is_truncated_to_recall_k():
    memory = FakeMemory(
        hits=[MemoryHit(f"fait {i}", 0.1, "fact") for i in range(10)]
    )
    coordinator = Coordinator(memory=memory, recall_k=5)
    open_session(coordinator)

    commands = say(coordinator, "salut")

    lines = memories(commands)[0].text.splitlines()[1:]
    assert len(lines) == 5


def test_recall_emits_no_extra_turns_or_session_commands():
    memory = FakeMemory(hits=[MemoryHit("bleu", 0.2, "journal")])
    coordinator = Coordinator(memory=memory)
    open_session(coordinator)

    commands = say(coordinator, "salut")

    assert effects(commands) == [
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
        SendMemoryToLlm(connection_id="s1", text="[MÉMOIRE]\n- bleu"),
    ]


def test_recall_decision_is_traced():
    memory = FakeMemory(hits=[MemoryHit("bleu", 0.2, "journal")])
    coordinator = Coordinator(memory=memory)
    open_session(coordinator)

    commands = say(coordinator, "salut")

    rappel = next(
        trace
        for trace in commands
        if isinstance(trace, Trace) and trace.kind == "Rappel"
    )
    assert rappel.source == "core"
    assert rappel.payload["connection_id"] == "s1"
    assert rappel.payload["query"] == "salut"
    assert rappel.payload["candidates"] == 1
    assert rappel.payload["injected"] == 1


def test_assistant_transcription_is_journaled_when_it_makes_the_session_visible():
    memory = FakeMemory()
    coordinator = Coordinator(memory=memory, user_id="alice")
    open_session(coordinator)

    asyncio.run(
        coordinator.handle(LlmTranscription(role=Role.ASSISTANT, text="bonjour"))
    )

    assert ("alice", "assistant", "bonjour") in memory.turns


def test_assistant_transcription_is_journaled_while_visible():
    memory = FakeMemory()
    coordinator = Coordinator(memory=memory, user_id="alice")
    open_session(coordinator)
    asyncio.run(coordinator.handle(LlmAudio(data=b"hi")))

    asyncio.run(
        coordinator.handle(LlmTranscription(role=Role.ASSISTANT, text="bonjour"))
    )

    assert ("alice", "assistant", "bonjour") in memory.turns


def test_recall_is_skipped_without_a_memory_store():
    coordinator = Coordinator()
    open_session(coordinator)

    commands = say(coordinator, "salut")

    assert memories(commands) == []
    assert effects(commands) == [
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT)
    ]


def test_recall_state_is_reset_on_a_new_session():
    memory = FakeMemory(hits=[MemoryHit("bleu", 0.2, "journal")])
    coordinator = Coordinator(memory=memory)
    open_session(coordinator, "c1")
    assert memories(say(coordinator, "un"))

    open_session(coordinator, "c2")

    assert memories(say(coordinator, "deux"))
    assert memory.queries[-1] == ("owner", "deux")


def test_recall_state_is_reset_on_teardown():
    memory = FakeMemory()
    coordinator = Coordinator(memory=memory)
    open_session(coordinator, "c1")
    say(coordinator, "un")
    asyncio.run(coordinator.handle(LlmClosed()))

    open_session(coordinator, "c1")
    say(coordinator, "deux")

    assert memory.queries[-1] == ("owner", "deux")
