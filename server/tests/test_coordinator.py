from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from protocol import SessionEndReason

from server.core import (
    IDLE_TIMEOUT,
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    ClientAudio,
    ClientInterrupt,
    ClientLink,
    ClientRegistered,
    ClientToolResult,
    Clock,
    CloseLiveConnection,
    Coordinator,
    Embedder,
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmOpened,
    LlmSocket,
    LlmToolCall,
    LlmTranscription,
    MemoryHit,
    MemoryStore,
    OpenLiveConnection,
    Role,
    SendAudioToClient,
    SendAudioToLlm,
    SendToolResultToLlm,
    SessionRequested,
    SessionState,
    ShutdownRequested,
    StartIdleTimer,
    TimerFired,
    ToolHandler,
    ToolRegistry,
    ToolSpec,
    Trace,
    TraceSink,
)


def drive(coordinator: Coordinator, events):
    async def run():
        commands = []
        for event in events:
            commands.extend(await coordinator.handle(event))
        return commands

    return asyncio.run(run())


def effects(commands):
    return [command for command in commands if not isinstance(command, Trace)]


def test_tap_then_athena_speaks_makes_session_visible():
    coordinator = Coordinator()
    commands = drive(
        coordinator,
        [
            ClientRegistered(client_id="c1"),
            SessionRequested(client_id="c1"),
            LlmOpened(),
            ClientAudio(data=b"mic"),
            LlmAudio(data=b"hello"),
            LlmClosed(),
        ],
    )

    assert effects(commands) == [
        OpenLiveConnection(connection_id="s1"),
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
        SendAudioToLlm(connection_id="s1", data=b"mic"),
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
        AnnounceSessionStarted(client_id="c1", auto=False),
        SendAudioToClient(client_id="c1", data=b"hello"),
        CancelIdleTimer(connection_id="s1"),
        CloseLiveConnection(connection_id="s1"),
        AnnounceSessionEnded(client_id="c1", reason=SessionEndReason.NORMAL),
    ]
    assert coordinator.state is SessionState.IDLE


def test_assistant_transcription_can_make_session_visible():
    coordinator = Coordinator()
    commands = drive(
        coordinator,
        [
            SessionRequested(client_id="c1"),
            LlmOpened(),
            LlmTranscription(role=Role.ASSISTANT, text="bonjour"),
            LlmAudio(data=b"later"),
        ],
    )

    assert effects(commands) == [
        OpenLiveConnection(connection_id="s1"),
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
        AnnounceSessionStarted(client_id="c1", auto=False),
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
        SendAudioToClient(client_id="c1", data=b"later"),
    ]


def test_user_transcription_does_not_make_session_visible():
    coordinator = Coordinator()
    commands = drive(
        coordinator,
        [
            SessionRequested(client_id="c1"),
            LlmOpened(),
            LlmTranscription(role=Role.USER, text="salut"),
        ],
    )

    assert AnnounceSessionStarted not in [type(c) for c in commands]
    assert coordinator.state is SessionState.LIVE_ACTIVE


def test_silent_live_connection_never_announces_a_session():
    coordinator = Coordinator()
    commands = drive(
        coordinator,
        [
            SessionRequested(client_id="c1"),
            LlmOpened(),
            LlmClosed(),
        ],
    )

    assert AnnounceSessionStarted not in [type(c) for c in commands]
    assert AnnounceSessionEnded not in [type(c) for c in commands]
    assert effects(commands) == [
        OpenLiveConnection(connection_id="s1"),
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
        CancelIdleTimer(connection_id="s1"),
        CloseLiveConnection(connection_id="s1"),
    ]


def test_each_handle_emits_at_least_one_trace():
    coordinator = Coordinator()
    events = [
        ClientRegistered(client_id="c1"),
        SessionRequested(client_id="c1"),
        LlmOpened(),
        ClientAudio(data=b"mic"),
        LlmAudio(data=b"hello"),
        LlmClosed(),
    ]

    for event in events:
        commands = asyncio.run(coordinator.handle(event))
        assert any(isinstance(command, Trace) for command in commands), event


def test_trace_carries_observability_shape():
    coordinator = Coordinator()
    asyncio.run(coordinator.handle(ClientRegistered(client_id="c1")))
    commands = asyncio.run(coordinator.handle(SessionRequested(client_id="c1")))
    trace = next(
        command
        for command in commands
        if isinstance(command, Trace) and command.kind == "SessionRequested"
    )

    assert trace.source == "core"
    assert trace.kind == "SessionRequested"
    assert trace.level == "info"
    assert trace.payload["connection_id"] == "s1"
    assert trace.payload["session_id"] == ""
    assert trace.payload["from_state"] == "Idle"
    assert trace.payload["to_state"] == "LiveConnecting"
    assert trace.payload["client_id"] == "c1"


def test_decision_trace_is_emitted_at_debug_level():
    coordinator = Coordinator()
    commands = asyncio.run(coordinator.handle(ClientRegistered(client_id="c1")))
    trace = next(command for command in commands if isinstance(command, Trace))

    assert trace.kind == "ClientRegistered"
    assert trace.level == "debug"


def test_client_tool_result_is_trace_only_and_leaves_state_unchanged():
    coordinator = Coordinator()
    commands = asyncio.run(
        coordinator.handle(
            ClientToolResult(tool_id="t1", name="shell", result_json=b"{}")
        )
    )
    assert effects(commands) == []
    assert coordinator.state is SessionState.IDLE
    assert any(isinstance(command, Trace) for command in commands)


def test_connection_ids_are_monotonic_and_deterministic():
    first = Coordinator()
    drive(
        first,
        [
            SessionRequested(client_id="c1"),
            LlmClosed(),
            SessionRequested(client_id="c1"),
        ],
    )
    assert first.connection_id == "s2"
    assert first.session_id == ""


def test_visibility_assigns_session_id_to_the_connection():
    coordinator = Coordinator()
    drive(
        coordinator,
        [
            SessionRequested(client_id="c1"),
            LlmOpened(),
            LlmAudio(data=b"hi"),
        ],
    )
    assert coordinator.connection_id == "s1"
    assert coordinator.session_id == "s1"


def test_events_and_commands_are_frozen_slots_dataclasses():
    types_to_check = [
        ClientRegistered,
        SessionRequested,
        ClientAudio,
        ClientInterrupt,
        ClientToolResult,
        LlmOpened,
        LlmAudio,
        LlmTranscription,
        LlmToolCall,
        LlmClosed,
        LlmFailed,
        ShutdownRequested,
        TimerFired,
        OpenLiveConnection,
        CloseLiveConnection,
        SendAudioToLlm,
        SendAudioToClient,
        SendToolResultToLlm,
        AnnounceSessionStarted,
        AnnounceSessionEnded,
        StartIdleTimer,
        CancelIdleTimer,
    ]

    for event_type in types_to_check:
        assert is_dataclass(event_type)
        assert "__slots__" in vars(event_type)
        assert event_type.__dataclass_params__.frozen  # type: ignore[attr-defined]


def test_frozen_dataclasses_reject_mutation():
    event = ClientAudio(data=b"x")
    with pytest.raises(FrozenInstanceError):
        event.data = b"y"  # type: ignore[misc]


def test_ports_exist():
    for port in (ClientLink, LlmSocket, Clock, TraceSink, MemoryStore, Embedder):
        assert isinstance(port, type)


def test_memory_and_tool_seams_are_frozen_slots_dataclasses():
    hit = MemoryHit(text="bleu", distance=0.2, source="journal")
    assert (hit.text, hit.distance, hit.source) == ("bleu", 0.2, "journal")

    spec = ToolSpec(
        name="save_memory", description="mémorise", parameters={"type": "object"}
    )
    assert spec.name == "save_memory"
    assert spec.parameters == {"type": "object"}

    for seam in (MemoryHit, ToolSpec):
        assert is_dataclass(seam)
        assert "__slots__" in vars(seam)
        assert seam.__dataclass_params__.frozen  # type: ignore[attr-defined]


class FakeMemory:
    def __init__(self, context: str = "") -> None:
        self.context = context
        self.users: list[str] = []

    async def session_context(self, user_id: str) -> str:
        self.users.append(user_id)
        return self.context

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        pass


def _opened(commands):
    return next(c for c in commands if isinstance(c, OpenLiveConnection))


def _activation(commands):
    return next(
        c for c in commands if isinstance(c, Trace) and c.kind == "Activation"
    )


def test_session_requested_activates_memory_context_into_the_instruction():
    memory = FakeMemory(context="FAIT ÉPINGLÉ")
    coordinator = Coordinator(
        memory=memory, user_id="alice", system_prompt="SYSTÈME"
    )

    commands = asyncio.run(coordinator.handle(SessionRequested(client_id="c1")))

    opened = _opened(commands)
    assert opened.instruction == "SYSTÈME\n\nFAIT ÉPINGLÉ"
    assert opened.tools == ()
    assert memory.users == ["alice"]


def test_session_requested_without_memory_uses_the_prompt_alone():
    coordinator = Coordinator(system_prompt="SYSTÈME")

    commands = asyncio.run(coordinator.handle(SessionRequested(client_id="c1")))

    assert _opened(commands).instruction == "SYSTÈME"


def test_session_requested_without_any_context_has_empty_instruction():
    coordinator = Coordinator()

    commands = asyncio.run(coordinator.handle(SessionRequested(client_id="c1")))

    assert _opened(commands).instruction == ""


def test_activation_is_traced_with_a_context_summary():
    memory = FakeMemory(context="X" * 12)
    coordinator = Coordinator(memory=memory, user_id="alice", system_prompt="S")

    commands = asyncio.run(coordinator.handle(SessionRequested(client_id="c1")))

    activation = _activation(commands)
    assert activation.source == "core"
    assert activation.level == "info"
    assert activation.payload["user_id"] == "alice"
    assert activation.payload["has_context"] is True
    assert activation.payload["context_chars"] == 12


def test_activation_traces_absence_of_context():
    coordinator = Coordinator(system_prompt="S")

    commands = asyncio.run(coordinator.handle(SessionRequested(client_id="c1")))

    activation = _activation(commands)
    assert activation.payload["has_context"] is False
    assert activation.payload["context_chars"] == 0


class FakeTools:
    def __init__(self, specs: tuple[ToolSpec, ...] = ()) -> None:
        self._specs = specs
        self.executed: list[tuple[str, object]] = []

    def specs(self) -> tuple[ToolSpec, ...]:
        return self._specs

    async def execute(self, name, arguments):
        self.executed.append((name, arguments))
        if name == "echo":
            return {"echo": arguments.get("value", "")}
        return {"status": "error", "message": f"unknown tool: {name}"}


def _tool_results(commands):
    return [c for c in commands if isinstance(c, SendToolResultToLlm)]


def test_session_requested_carries_the_registered_tool_declarations():
    spec = ToolSpec(name="echo", description="écho", parameters={"type": "object"})
    coordinator = Coordinator(tools=FakeTools(specs=(spec,)))

    commands = asyncio.run(coordinator.handle(SessionRequested(client_id="c1")))

    assert _opened(commands).tools == (spec,)


def test_tool_call_executes_the_handler_and_emits_the_result():
    registry = FakeTools()
    coordinator = Coordinator(tools=registry)
    drive(coordinator, [SessionRequested(client_id="c1"), LlmOpened()])

    commands = asyncio.run(
        coordinator.handle(
            LlmToolCall(tool_id="t1", name="echo", arguments={"value": "bonjour"})
        )
    )

    assert registry.executed == [("echo", {"value": "bonjour"})]
    assert _tool_results(commands) == [
        SendToolResultToLlm(
            connection_id="s1",
            tool_id="t1",
            name="echo",
            result={"echo": "bonjour"},
        )
    ]
    assert any(isinstance(c, Trace) for c in commands)


def test_unknown_tool_call_yields_a_traced_error_without_crashing():
    coordinator = Coordinator(tools=FakeTools())
    drive(coordinator, [SessionRequested(client_id="c1"), LlmOpened()])

    commands = asyncio.run(
        coordinator.handle(LlmToolCall(tool_id="t9", name="ghost", arguments={}))
    )

    assert _tool_results(commands) == [
        SendToolResultToLlm(
            connection_id="s1",
            tool_id="t9",
            name="ghost",
            result={"status": "error", "message": "unknown tool: ghost"},
        )
    ]
    assert any(isinstance(c, Trace) for c in commands)
    assert coordinator.state is SessionState.LIVE_ACTIVE


def test_tool_call_without_registry_is_a_traced_error_without_crashing():
    coordinator = Coordinator()
    drive(coordinator, [SessionRequested(client_id="c1"), LlmOpened()])

    commands = asyncio.run(
        coordinator.handle(LlmToolCall(tool_id="t1", name="echo", arguments={}))
    )

    results = _tool_results(commands)
    assert results and results[0].result["status"] == "error"
    assert any(isinstance(c, Trace) for c in commands)

