from __future__ import annotations

import asyncio

from server.core import (
    Coordinator,
    LlmOpened,
    LlmToolCall,
    OpenLiveConnection,
    SendToolResultToLlm,
    SessionRequested,
    ToolRegistry,
    Trace,
)
from server.core.memory_tools import register_memory_tools


class FakeMemoryStore:
    """Pure, in-process ``MemoryStore`` double for the kernel policy tests."""

    def __init__(self) -> None:
        self.saved: list[tuple[str, str, bool]] = []
        self.forgotten: list[tuple[str, str, float]] = []
        self._facts: list[tuple[str, bool]] = []
        self.turns: list[tuple[str, str, str]] = []

    async def session_context(self, user_id: str) -> str:
        pinned = [content for content, is_pinned in self._facts if is_pinned]
        if not pinned:
            return ""
        return "Profil:\n" + "\n".join(f"- {content}" for content in pinned)

    async def pinned_facts(self, user_id: str) -> list[str]:
        return [content for content, is_pinned in self._facts if is_pinned]

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        self.turns.append((user_id, role, text))

    async def recall(self, user_id: str, query: str):
        return []

    async def save(self, user_id: str, content: str, *, pinned: bool) -> None:
        self.saved.append((user_id, content, pinned))
        self._facts.append((content, pinned))

    async def forget(self, user_id: str, query: str, max_distance: float) -> bool:
        self.forgotten.append((user_id, query, max_distance))
        for index, (content, _pinned) in enumerate(self._facts):
            if query in content or content in query:
                del self._facts[index]
                return True
        return False


def _registry(memory: FakeMemoryStore, *, forget_max_distance: float = 0.6):
    registry = ToolRegistry()
    register_memory_tools(
        registry, memory, "alice", forget_max_distance=forget_max_distance
    )
    return registry


def _drive(coordinator: Coordinator, events) -> list:
    async def run():
        commands: list = []
        for event in events:
            commands.extend(await coordinator.handle(event))
        return commands

    return asyncio.run(run())


def _open_session(coordinator: Coordinator) -> list:
    return _drive(
        coordinator, [SessionRequested(client_id="c1"), LlmOpened()]
    )


def _tool_call(name: str, arguments: dict) -> LlmToolCall:
    return LlmToolCall(tool_id="t1", name=name, arguments=arguments)


def _results(commands) -> list[SendToolResultToLlm]:
    return [c for c in commands if isinstance(c, SendToolResultToLlm)]


def test_registers_exactly_the_two_kernel_memory_tools() -> None:
    registry = _registry(FakeMemoryStore())

    names = {spec.name for spec in registry.specs()}

    assert names == {"save_memory", "forget_memory"}


def test_save_memory_profile_writes_a_pinned_fact_and_emits_the_result() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    commands = _drive(
        coordinator, [_tool_call("save_memory", {"content": "aime le bleu", "kind": "profile"})]
    )

    results = _results(commands)
    assert len(results) == 1
    assert results[0].result["status"] == "ok"
    assert results[0].result["pinned"] is True
    assert memory.saved == [("alice", "aime le bleu", True)]


def test_save_memory_fact_is_not_pinned() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    results = _results(
        _drive(
            coordinator,
            [_tool_call("save_memory", {"content": "habite à Lyon", "kind": "fact"})],
        )
    )

    assert results[0].result["pinned"] is False
    assert memory.saved == [("alice", "habite à Lyon", False)]


def test_forget_memory_removes_the_nearest_fact_and_reports_it() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)
    _drive(
        coordinator,
        [_tool_call("save_memory", {"content": "est végétarien", "kind": "profile"})],
    )

    results = _results(
        _drive(coordinator, [_tool_call("forget_memory", {"content": "végétarien"})])
    )

    assert results[0].result["status"] == "removed"
    assert memory.forgotten == [("alice", "végétarien", 0.6)]


def test_forget_memory_reports_when_nothing_matches() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    results = _results(
        _drive(coordinator, [_tool_call("forget_memory", {"content": "inconnu"})])
    )

    assert results[0].result["status"] == "not_found"


def test_a_saved_profile_is_injected_on_the_next_session() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(
        memory=memory, user_id="alice", system_prompt="SYSTÈME", tools=_registry(memory)
    )
    _open_session(coordinator)
    _drive(
        coordinator,
        [_tool_call("save_memory", {"content": "aime le bleu", "kind": "profile"})],
    )

    commands = _open_session(coordinator)

    opened = next(c for c in commands if isinstance(c, OpenLiveConnection))
    assert "aime le bleu" in opened.instruction


def test_save_memory_without_content_is_a_non_fatal_error() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    results = _results(_drive(coordinator, [_tool_call("save_memory", {})]))

    assert results[0].result["status"] == "error"
    assert memory.saved == []


def test_save_memory_coerces_loose_arguments() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    _drive(
        coordinator,
        [_tool_call("save_memory", {"content": 42, "kind": "Profile"})],
    )

    assert memory.saved == [("alice", "42", True)]


def test_save_memory_with_an_unknown_kind_is_rejected_without_writing() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    results = _results(
        _drive(
            coordinator,
            [_tool_call("save_memory", {"content": "x", "kind": "préférence"})],
        )
    )

    assert results[0].result["status"] == "error"
    assert memory.saved == []


def test_save_memory_with_a_non_string_kind_is_rejected_without_writing() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    results = _results(
        _drive(coordinator, [_tool_call("save_memory", {"content": "x", "kind": 7})])
    )

    assert results[0].result["status"] == "error"
    assert memory.saved == []


def test_save_memory_without_a_kind_is_rejected_without_writing() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    results = _results(
        _drive(coordinator, [_tool_call("save_memory", {"content": "x"})])
    )

    assert results[0].result["status"] == "error"
    assert memory.saved == []



def test_forget_memory_without_content_is_a_non_fatal_error() -> None:
    memory = FakeMemoryStore()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    results = _results(_drive(coordinator, [_tool_call("forget_memory", {})]))

    assert results[0].result["status"] == "error"
    assert memory.forgotten == []


def test_a_memory_failure_never_breaks_the_tool_round_trip() -> None:
    class ExplodingMemory(FakeMemoryStore):
        async def forget(self, user_id: str, query: str, max_distance: float) -> bool:
            raise RuntimeError("boom")

    memory = ExplodingMemory()
    coordinator = Coordinator(memory=memory, user_id="alice", tools=_registry(memory))
    _open_session(coordinator)

    results = _results(
        _drive(coordinator, [_tool_call("forget_memory", {"content": "x"})])
    )

    assert results[0].result["status"] == "error"
