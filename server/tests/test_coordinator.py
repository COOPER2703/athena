from __future__ import annotations

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
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmOpened,
    LlmSocket,
    LlmTranscription,
    OpenLiveConnection,
    Role,
    SendAudioToClient,
    SendAudioToLlm,
    SessionRequested,
    SessionState,
    ShutdownRequested,
    StartIdleTimer,
    TimerFired,
    Trace,
    TraceSink,
)


def drive(coordinator: Coordinator, events):
    commands = []
    for event in events:
        commands.extend(coordinator.handle(event))
    return commands


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
        commands = coordinator.handle(event)
        assert any(isinstance(command, Trace) for command in commands), event


def test_trace_carries_observability_shape():
    coordinator = Coordinator()
    coordinator.handle(ClientRegistered(client_id="c1"))
    commands = coordinator.handle(SessionRequested(client_id="c1"))
    trace = next(command for command in commands if isinstance(command, Trace))

    assert trace.source == "core"
    assert trace.kind == "SessionRequested"
    assert trace.level == "info"
    assert trace.payload["connection_id"] == "s1"
    assert trace.payload["session_id"] == ""
    assert trace.payload["from_state"] == "Idle"
    assert trace.payload["to_state"] == "LiveConnecting"
    assert trace.payload["client_id"] == "c1"


def test_client_tool_result_is_trace_only_and_leaves_state_unchanged():
    coordinator = Coordinator()
    commands = coordinator.handle(
        ClientToolResult(tool_id="t1", name="shell", result_json=b"{}")
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
        LlmClosed,
        LlmFailed,
        ShutdownRequested,
        TimerFired,
        OpenLiveConnection,
        CloseLiveConnection,
        SendAudioToLlm,
        SendAudioToClient,
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
    for port in (ClientLink, LlmSocket, Clock, TraceSink):
        assert isinstance(port, type)
