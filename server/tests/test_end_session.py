"""Tool du noyau ``end_session`` et clôture en attente (#130).

On n'observe que le comportement externe du ``Coordinator`` : les Commandes
émises, l'état de la Session et les Traces. Aucun timer réel : l'échéance de la
grâce est injectée par ``TimerFired`` sur la clé de la Connexion, exactement ce
que fera l'horloge. Le Tool est exercé à travers un vrai ``ToolRegistry``.
"""

from __future__ import annotations

from conftest import drive, effects, handle, opened, traces, visible_session
from protocol import SessionEndReason
from server.core import (
    AnnounceSessionEnded,
    CancelIdleTimer,
    CloseLiveConnection,
    Coordinator,
    LlmAudio,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
    LlmToolCall,
    SendToolResultToLlm,
    SessionRequested,
    SessionState,
    StartIdleTimer,
    TimerFired,
    ToolRegistry,
)
from server.core.coordinator import END_SESSION_GRACE_TIMEOUT, IDLE_TIMEOUT
from server.core.session_tools import END_SESSION, register_session_tools


def _coordinator() -> Coordinator:
    registry = ToolRegistry()
    register_session_tools(registry)
    return Coordinator(tools=registry)


def _call(coordinator: Coordinator, tool_id: str = "t1") -> list:
    return handle(
        coordinator,
        LlmToolCall(tool_id=tool_id, name=END_SESSION, arguments={}),
    )


def _start_timers(commands) -> list[StartIdleTimer]:
    return [c for c in effects(commands) if isinstance(c, StartIdleTimer)]


def test_ordinary_session_declares_end_session_before_visibility():
    """Le Tool est déclaré dès l'ouverture, avant que la Session ne soit visible."""
    coordinator = _coordinator()

    commands = drive(coordinator, [SessionRequested(client_id="c1")])

    names = [spec.name for spec in opened(commands).tools]
    assert END_SESSION in names


def test_end_session_replies_before_opening_the_pending_close():
    """Le résultat part d'abord, puis la grâce de 3 s ; aucune fermeture immédiate."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())

    commands = _call(coordinator)

    assert effects(commands) == [
        SendToolResultToLlm(
            connection_id="s1",
            tool_id="t1",
            name=END_SESSION,
            result={"status": "ok"},
        ),
        StartIdleTimer(
            connection_id="s1", timeout=END_SESSION_GRACE_TIMEOUT
        ),
    ]
    assert END_SESSION_GRACE_TIMEOUT == 3.0
    assert coordinator.state is SessionState.SESSION_VISIBLE
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]


def test_grace_expiry_closes_the_session_as_completed():
    """À l'échéance, la Session se ferme par le chemin unique, motif COMPLETED."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)

    commands = handle(coordinator, TimerFired(connection_id="s1"))

    assert effects(commands) == [
        CancelIdleTimer(connection_id="s1"),
        CloseLiveConnection(connection_id="s1"),
        AnnounceSessionEnded(
            client_id="c1", reason=SessionEndReason.COMPLETED
        ),
    ]
    assert coordinator.state is SessionState.IDLE
    closed = [trace for trace in traces(commands) if trace.kind == "SessionClosed"]
    assert closed[0].payload["reason"] == "COMPLETED"


def test_llm_audio_during_pending_rearms_the_grace_not_the_idle_timeout():
    """Chaque audio du modèle relance 3 s : jamais le timeout de 30 s."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)

    commands = handle(coordinator, LlmAudio(data=b"bonne nuit"))

    assert _start_timers(commands) == [
        StartIdleTimer(
            connection_id="s1", timeout=END_SESSION_GRACE_TIMEOUT
        )
    ]
    assert coordinator.state is SessionState.SESSION_VISIBLE


def test_end_session_at_rest_is_an_error_and_arms_no_timer():
    coordinator = _coordinator()

    commands = _call(coordinator)

    results = [c for c in commands if isinstance(c, SendToolResultToLlm)]
    assert results[0].result["status"] == "error"
    assert _start_timers(commands) == []
    assert coordinator.state is SessionState.IDLE


def test_end_session_while_connecting_is_an_error_and_arms_no_timer():
    """``LIVE_CONNECTING`` n'est pas encore une Session active."""
    coordinator = _coordinator()
    drive(coordinator, [SessionRequested(client_id="c1")])

    commands = _call(coordinator)

    results = [c for c in commands if isinstance(c, SendToolResultToLlm)]
    assert results[0].result["status"] == "error"
    assert _start_timers(commands) == []
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]


def test_second_end_session_while_pending_succeeds_without_extending_the_grace():
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator, tool_id="t1")

    commands = _call(coordinator, tool_id="t2")

    results = [c for c in commands if isinstance(c, SendToolResultToLlm)]
    assert results[0].result == {"status": "ok"}
    assert _start_timers(commands) == []
    assert coordinator.state is SessionState.SESSION_VISIBLE


def test_another_teardown_clears_the_pending_close():
    """Une fin pour une autre cause n'est jamais convertie en COMPLETED."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)

    commands = handle(
        coordinator, LlmFailed(cause=LlmFailureCause.ERROR, connection_id="s1")
    )

    assert coordinator.state is SessionState.IDLE
    closed = [trace for trace in traces(commands) if trace.kind == "SessionClosed"]
    assert closed[0].payload["reason"] == "ERROR"
    # Aucun timer périmé ne peut refermer quoi que ce soit.
    stale = handle(coordinator, TimerFired(connection_id="s1"))
    assert coordinator.state is SessionState.IDLE
    assert not [c for c in stale if isinstance(c, CloseLiveConnection)]


def test_pending_close_does_not_leak_into_the_next_session():
    """Une nouvelle Session repart vierge : son audio réarme 30 s, pas 3 s."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)
    handle(coordinator, LlmFailed(cause=LlmFailureCause.ERROR, connection_id="s1"))

    drive(coordinator, [SessionRequested(client_id="c2"), LlmOpened()])
    commands = handle(coordinator, LlmAudio(data=b"bonjour"))

    assert _start_timers(commands) == [
        StartIdleTimer(connection_id="s2", timeout=IDLE_TIMEOUT)
    ]
