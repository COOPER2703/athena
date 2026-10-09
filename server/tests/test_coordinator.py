from __future__ import annotations

from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from protocol import SessionEndReason

from server.core import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    ClientAudio,
    ClientInterrupt,
    ClientLink,
    ClientRegistered,
    Clock,
    CloseLiveConnection,
    Coordinator,
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
    LlmSocket,
    LlmTranscription,
    OpenLiveConnection,
    Role,
    SendAudioToClient,
    SendAudioToLlm,
    SessionRequested,
    SessionState,
    StartIdleTimer,
    CancelIdleTimer,
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
        OpenLiveConnection(session_id="s1"),
        SendAudioToLlm(session_id="s1", data=b"mic"),
        AnnounceSessionStarted(client_id="c1", auto=False),
        SendAudioToClient(client_id="c1", data=b"hello"),
        CloseLiveConnection(session_id="s1"),
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
        OpenLiveConnection(session_id="s1"),
        AnnounceSessionStarted(client_id="c1", auto=False),
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
    assert effects(commands) == [
        OpenLiveConnection(session_id="s1"),
        CloseLiveConnection(session_id="s1"),
        AnnounceSessionEnded(client_id="c1", reason=SessionEndReason.NORMAL),
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
    assert trace.payload["session_id"] == "s1"
    assert trace.payload["from_state"] == "Idle"
    assert trace.payload["to_state"] == "LiveConnecting"
    assert trace.payload["client_id"] == "c1"


def test_session_ids_are_monotonic_and_deterministic():
    first = Coordinator()
    drive(
        first,
        [
            SessionRequested(client_id="c1"),
            LlmClosed(),
            SessionRequested(client_id="c1"),
        ],
    )
    assert first.session_id == "s2"


def test_events_and_commands_are_frozen_slots_dataclasses():
    types_to_check = [
        ClientRegistered,
        SessionRequested,
        ClientAudio,
        ClientInterrupt,
        LlmOpened,
        LlmAudio,
        LlmTranscription,
        LlmClosed,
        LlmFailed,
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


@pytest.mark.parametrize(
    "event",
    [
        ClientInterrupt(client_id="c1"),
        TimerFired(session_id="s1"),
        LlmFailed(cause=LlmFailureCause.ERROR),
    ],
)
def test_policy_events_are_recorded_without_acting(event):
    coordinator = Coordinator()
    drive(coordinator, [SessionRequested(client_id="c1"), LlmOpened()])

    commands = coordinator.handle(event)

    assert effects(commands) == []
    assert any(isinstance(command, Trace) for command in commands)
    assert coordinator.state is SessionState.LIVE_ACTIVE


def test_llm_failed_trace_distinguishes_go_away_from_error():
    coordinator = Coordinator()
    coordinator.handle(SessionRequested(client_id="c1"))
    coordinator.handle(LlmOpened())

    for cause, expected in (
        (LlmFailureCause.ERROR, "error"),
        (LlmFailureCause.GO_AWAY, "go_away"),
    ):
        trace = coordinator.handle(LlmFailed(cause=cause))[0]
        assert isinstance(trace, Trace)
        assert trace.payload["cause"] == expected

