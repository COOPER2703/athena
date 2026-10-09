from __future__ import annotations

import pytest

from protocol import SessionEndReason

from server.core import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    ClientAudio,
    ClientInterrupt,
    CloseLiveConnection,
    Coordinator,
    LlmAudio,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
    LlmTranscription,
    OpenLiveConnection,
    Role,
    SendAudioToClient,
    SendAudioToLlm,
    SessionRequested,
    SessionState,
    StartIdleTimer,
    TimerFired,
    Trace,
)


def drive(coordinator, events):
    commands = []
    for event in events:
        commands.extend(coordinator.handle(event))
    return commands


def effects(commands):
    return [command for command in commands if not isinstance(command, Trace)]


def traces(commands):
    return [command for command in commands if isinstance(command, Trace)]


def live_session(client_id="c1", speaking=True):
    events = [SessionRequested(client_id=client_id), LlmOpened()]
    if speaking:
        events.append(LlmAudio(data=b"hi"))
    return events


def test_timer_is_armed_when_session_requested_even_before_any_speech():
    coordinator = Coordinator()
    commands = coordinator.handle(SessionRequested(client_id="c1"))

    assert effects(commands) == [
        OpenLiveConnection(session_id="s1"),
        StartIdleTimer(session_id="s1"),
    ]


def test_idle_timeout_tears_down_current_session():
    coordinator = Coordinator()
    drive(coordinator, live_session())

    commands = coordinator.handle(TimerFired(session_id="s1"))

    assert effects(commands) == [
        CancelIdleTimer(session_id="s1"),
        CloseLiveConnection(session_id="s1"),
        AnnounceSessionEnded(client_id="c1", reason=SessionEndReason.TIMEOUT),
    ]
    assert coordinator.state is SessionState.IDLE


def test_user_transcription_rearms_idle_timer():
    coordinator = Coordinator()
    drive(coordinator, live_session(speaking=False))

    commands = coordinator.handle(LlmTranscription(role=Role.USER, text="salut"))

    assert StartIdleTimer(session_id="s1") in effects(commands)


def test_llm_audio_rearms_idle_timer():
    coordinator = Coordinator()
    drive(coordinator, live_session())

    commands = coordinator.handle(LlmAudio(data=b"more"))

    assert StartIdleTimer(session_id="s1") in effects(commands)


def test_client_audio_does_not_rearm_idle_timer():
    coordinator = Coordinator()
    drive(coordinator, live_session(speaking=False))

    commands = coordinator.handle(ClientAudio(data=b"mic"))

    assert effects(commands) == [SendAudioToLlm(session_id="s1", data=b"mic")]


def test_assistant_transcription_does_not_rearm_idle_timer():
    coordinator = Coordinator()
    drive(coordinator, live_session(speaking=False))

    commands = coordinator.handle(LlmTranscription(role=Role.ASSISTANT, text="bonjour"))

    assert effects(commands) == [
        AnnounceSessionStarted(client_id="c1", auto=False)
    ]


def test_barge_in_keeps_session_visible_without_ending_it():
    coordinator = Coordinator()
    drive(coordinator, live_session())

    commands = coordinator.handle(ClientInterrupt(client_id="c1"))

    assert effects(commands) == []
    assert coordinator.state is SessionState.SESSION_VISIBLE
    barge_in = next(trace for trace in traces(commands) if trace.kind == "barge_in")
    assert barge_in.payload["from_state"] == "SessionVisible"
    assert barge_in.payload["to_state"] == "SessionVisible"


def test_second_session_request_replaces_the_first_before_opening_a_new_one():
    coordinator = Coordinator()
    drive(coordinator, live_session(client_id="c1"))

    commands = coordinator.handle(SessionRequested(client_id="c2"))

    eff = effects(commands)
    assert eff == [
        CancelIdleTimer(session_id="s1"),
        CloseLiveConnection(session_id="s1"),
        AnnounceSessionEnded(client_id="c1", reason=SessionEndReason.REPLACED),
        OpenLiveConnection(session_id="s2"),
        StartIdleTimer(session_id="s2"),
    ]
    assert eff.index(
        AnnounceSessionEnded(client_id="c1", reason=SessionEndReason.REPLACED)
    ) < eff.index(OpenLiveConnection(session_id="s2"))
    assert coordinator.session_id == "s2"
    assert coordinator.state is SessionState.LIVE_CONNECTING


def test_replacement_never_leaves_two_live_connections():
    coordinator = Coordinator()
    drive(coordinator, live_session(client_id="c1"))

    eff = effects(coordinator.handle(SessionRequested(client_id="c2")))

    assert sum(isinstance(c, OpenLiveConnection) for c in eff) == 1
    assert sum(isinstance(c, CloseLiveConnection) for c in eff) == 1


@pytest.mark.parametrize(
    "cause", [LlmFailureCause.ERROR, LlmFailureCause.GO_AWAY]
)
def test_llm_failed_closes_connection_then_ends_session_with_error(cause):
    coordinator = Coordinator()
    drive(coordinator, live_session())

    commands = coordinator.handle(LlmFailed(cause=cause))

    assert effects(commands) == [
        CancelIdleTimer(session_id="s1"),
        CloseLiveConnection(session_id="s1"),
        AnnounceSessionEnded(client_id="c1", reason=SessionEndReason.ERROR),
    ]
    assert coordinator.state is SessionState.IDLE
    assert coordinator.session_id == ""


def test_llm_failed_trace_distinguishes_go_away_from_raw_error():
    def teardown_trace(cause):
        coordinator = Coordinator()
        drive(coordinator, live_session())
        commands = coordinator.handle(LlmFailed(cause=cause))
        return next(
            trace
            for trace in traces(commands)
            if trace.kind == "LlmFailed"
        )

    assert teardown_trace(LlmFailureCause.ERROR).payload["cause"] == "error"
    assert teardown_trace(LlmFailureCause.GO_AWAY).payload["cause"] == "go_away"


def test_llm_failed_before_session_is_visible_still_ends_with_error():
    coordinator = Coordinator()
    drive(coordinator, [SessionRequested(client_id="c1")])

    commands = coordinator.handle(LlmFailed(cause=LlmFailureCause.ERROR))

    assert AnnounceSessionEnded(
        client_id="c1", reason=SessionEndReason.ERROR
    ) in effects(commands)
    assert coordinator.state is SessionState.IDLE


def test_llm_failed_leaves_no_residual_commands_or_zombie_session():
    coordinator = Coordinator()
    drive(coordinator, live_session())

    commands = coordinator.handle(LlmFailed(cause=LlmFailureCause.GO_AWAY))

    assert all(
        isinstance(command, (CancelIdleTimer, CloseLiveConnection, AnnounceSessionEnded, Trace))
        for command in commands
    )
    assert coordinator.state is SessionState.IDLE


def test_stale_timer_after_replacement_does_not_end_the_new_session():
    coordinator = Coordinator()
    drive(coordinator, [SessionRequested(client_id="c1"), LlmOpened()])
    coordinator.handle(SessionRequested(client_id="c2"))

    commands = coordinator.handle(TimerFired(session_id="s1"))

    assert effects(commands) == []
    assert coordinator.state is SessionState.LIVE_CONNECTING
    assert coordinator.session_id == "s2"


def test_current_timer_after_replacement_ends_the_new_session():
    coordinator = Coordinator()
    drive(coordinator, [SessionRequested(client_id="c1"), LlmOpened()])
    coordinator.handle(SessionRequested(client_id="c2"))

    commands = coordinator.handle(TimerFired(session_id="s2"))

    assert AnnounceSessionEnded(
        client_id="c2", reason=SessionEndReason.TIMEOUT
    ) in effects(commands)
    assert coordinator.state is SessionState.IDLE
