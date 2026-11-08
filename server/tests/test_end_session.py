"""Tool du noyau ``end_session`` et fermeture immédiate (#130, ADR-0022).

On n'observe que le comportement externe du ``Coordinator`` : les Commandes
émises, l'état de la Session et les Traces. Aucun timer réel : la fermeture est
immédiate, donc aucun ``TimerFired`` n'est nécessaire. Le Tool est exercé à
travers un vrai ``ToolRegistry``.
"""

from __future__ import annotations

from conftest import (
    FakeJudge,
    announcement,
    close_window,
    drive,
    effects,
    handle,
    opened,
    traces,
    visible_session,
)
from protocol import SessionEndReason
from server.core import (
    AnnounceSessionEnded,
    CancelIdleTimer,
    ClientInterrupt,
    CloseLiveConnection,
    Coordinator,
    EventReceived,
    LlmAudio,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
    LlmToolCall,
    LlmTranscription,
    MemoryHit,
    Role,
    SendMemoryToLlm,
    SendToolResultToLlm,
    SessionRequested,
    SessionState,
    StartIdleTimer,
    TimerFired,
    ToolRegistry,
)
from server.core.coordinator import IDLE_TIMEOUT
from server.core.session_tools import END_SESSION, register_session_tools


def _coordinator(memory=None) -> Coordinator:
    registry = ToolRegistry()
    register_session_tools(registry)
    return Coordinator(tools=registry, memory=memory, user_id="alice")


class FakeMemory:
    """Double pur de ``MemoryStore`` : Journal et Rappel observables."""

    def __init__(self, hits: list[MemoryHit] | None = None) -> None:
        self._hits = list(hits or [])
        self.turns: list[tuple[str, str, str]] = []
        self.queries: list[tuple[str, str]] = []

    async def session_context(self, user_id: str) -> str:
        return ""

    async def pinned_facts(self, user_id: str) -> list[str]:
        return []

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        self.turns.append((user_id, role, text))

    async def recall(self, user_id: str, query: str) -> list[MemoryHit]:
        self.queries.append((user_id, query))
        return list(self._hits)


def _call(coordinator: Coordinator, tool_id: str = "t1") -> list:
    return handle(
        coordinator,
        LlmToolCall(tool_id=tool_id, name=END_SESSION, arguments={}),
    )


def _start_timers(commands) -> list[StartIdleTimer]:
    return [c for c in effects(commands) if isinstance(c, StartIdleTimer)]


def _announcement_coordinator() -> Coordinator:
    """Coordinator entrant en Annonce : juge fixe, prompt d'Annonce."""
    registry = ToolRegistry()
    register_session_tools(registry)
    return Coordinator(
        tools=registry,
        memory=None,
        user_id="alice",
        announce_prompt="JUGE",
        judge=FakeJudge(message="Porte ouverte"),
    )


def _judging_connection(coordinator: Coordinator) -> None:
    """Ouvre la Connexion de jugement, active mais pas encore visible."""
    drive(coordinator, [EventReceived(source="matrix", content="porte ouverte")])
    commands = close_window(coordinator)
    assert announcement(commands).text == "Porte ouverte"
    handle(coordinator, LlmOpened())
    assert coordinator.state is SessionState.LIVE_ACTIVE


def test_ordinary_session_declares_end_session_before_visibility():
    """Le Tool est déclaré dès l'ouverture, avant que la Session ne soit visible."""
    coordinator = _coordinator()

    commands = drive(coordinator, [SessionRequested(client_id="c1")])

    names = [spec.name for spec in opened(commands).tools]
    assert END_SESSION in names


def test_end_session_description_lets_the_model_judge_implicit_goodbyes():
    """Le modèle juge le sens : « non c'est bon en fait » suffit, sans formule exacte."""
    registry = ToolRegistry()
    register_session_tools(registry)
    spec = next(spec for spec in registry.specs() if spec.name == END_SESSION)

    description = spec.description.lower()
    assert "c'est bon" in description
    assert "en fait" in description
    assert "immédiatement" in description
    assert "doute" in description


def test_end_session_replies_then_closes_immediately_as_completed():
    """Le résultat part au modèle, puis la Session se ferme sans aucun timer."""
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
        CancelIdleTimer(connection_id="s1"),
        CloseLiveConnection(connection_id="s1"),
        AnnounceSessionEnded(
            client_id="c1", reason=SessionEndReason.COMPLETED
        ),
    ]
    assert _start_timers(commands) == []
    assert coordinator.state is SessionState.IDLE
    closed = [trace for trace in traces(commands) if trace.kind == "SessionClosed"]
    assert closed[0].payload["reason"] == "COMPLETED"
    assert closed[0].payload["connection_id"] == "s1"
    assert closed[0].payload["session_id"] == "s1"


def test_end_session_after_only_opening_still_returns_the_client_to_listening():
    """Une Session jamais visible ferme quand même : le Client doit revenir à LISTENING."""
    coordinator = _coordinator()
    drive(coordinator, [SessionRequested(client_id="c1"), LlmOpened()])
    assert coordinator.state is SessionState.LIVE_ACTIVE

    commands = _call(coordinator)

    assert AnnounceSessionEnded(
        client_id="c1", reason=SessionEndReason.COMPLETED
    ) in effects(commands)
    assert CloseLiveConnection(connection_id="s1") in effects(commands)
    assert coordinator.state is SessionState.IDLE


def test_end_session_at_rest_is_an_error_and_arms_no_timer():
    coordinator = _coordinator()

    commands = _call(coordinator)

    results = [c for c in commands if isinstance(c, SendToolResultToLlm)]
    assert results[0].result["status"] == "error"
    assert _start_timers(commands) == []
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]
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


def test_end_session_on_a_judging_connection_is_refused_without_closing():
    """La Connexion de jugement n'est pas une Session ordinaire : le Tool est refusé."""
    coordinator = _announcement_coordinator()
    _judging_connection(coordinator)

    commands = _call(coordinator)

    results = [c for c in commands if isinstance(c, SendToolResultToLlm)]
    assert results[0].result["status"] == "error"
    assert _start_timers(commands) == []
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]
    assert not [c for c in commands if isinstance(c, AnnounceSessionEnded)]
    assert coordinator.state is SessionState.LIVE_ACTIVE


def test_end_session_on_a_visible_announcement_is_refused_without_closing():
    """Une Annonce visible reste sans Tools : ``end_session`` n'y ferme rien."""
    coordinator = _announcement_coordinator()
    _judging_connection(coordinator)
    drive(coordinator, [LlmAudio(data=b"parle")])
    assert coordinator.state is SessionState.SESSION_VISIBLE

    commands = _call(coordinator)

    results = [c for c in commands if isinstance(c, SendToolResultToLlm)]
    assert results[0].result["status"] == "error"
    assert _start_timers(commands) == []
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]
    assert not [c for c in commands if isinstance(c, AnnounceSessionEnded)]
    assert coordinator.state is SessionState.SESSION_VISIBLE


def test_second_end_session_after_close_is_refused_at_rest():
    """La Session est déjà fermée : un second appel est une erreur, pas une clôture."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator, tool_id="t1")
    assert coordinator.state is SessionState.IDLE

    commands = _call(coordinator, tool_id="t2")

    results = [c for c in commands if isinstance(c, SendToolResultToLlm)]
    assert results[0].result["status"] == "error"
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]
    assert coordinator.state is SessionState.IDLE


def test_barge_in_does_not_close_or_cancel_anything():
    """Sans clôture en attente, un barge-in reste un barge-in ordinaire."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())

    commands = handle(coordinator, ClientInterrupt(client_id="c1"))

    assert effects(commands) == []
    assert coordinator.state is SessionState.SESSION_VISIBLE
    assert [trace.kind for trace in traces(commands)] == ["barge_in"]
    assert not [
        trace for trace in traces(commands) if trace.kind == "SessionEndCanceled"
    ]


def test_llm_audio_rearms_the_idle_timeout():
    """L'audio du modèle relance toujours le timeout d'inactivité de 30 s."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())

    commands = handle(coordinator, LlmAudio(data=b"bonne nuit"))

    assert _start_timers(commands) == [
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT)
    ]
    assert coordinator.state is SessionState.SESSION_VISIBLE


def test_user_transcription_rearms_the_idle_timeout_and_recalls():
    """Une parole utilisateur réarme 30 s, journalise et rappelle comme avant."""
    memory = FakeMemory(
        hits=[MemoryHit(text="bleu", distance=0.1, source="journal")]
    )
    coordinator = _coordinator(memory=memory)
    drive(coordinator, visible_session())

    commands = handle(
        coordinator,
        LlmTranscription(text="encore une question", role=Role.USER),
    )

    assert _start_timers(commands) == [
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT)
    ]
    assert memory.turns == [("alice", "user", "encore une question")]
    assert memory.queries == [("alice", "encore une question")]
    recalled = [c for c in commands if isinstance(c, SendMemoryToLlm)]
    assert len(recalled) == 1
    assert "bleu" in recalled[0].text
    assert coordinator.state is SessionState.SESSION_VISIBLE


def test_new_session_after_completed_close_starts_clean():
    """Une nouvelle Session repart vierge : son audio réarme 30 s."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)

    drive(
        coordinator,
        [SessionRequested(client_id="c2"), LlmOpened(), LlmAudio(data=b"bonjour")],
    )
    assert coordinator.connection_id == "s2"

    commands = handle(coordinator, LlmAudio(data=b"encore"))

    assert _start_timers(commands) == [
        StartIdleTimer(connection_id="s2", timeout=IDLE_TIMEOUT)
    ]
    assert coordinator.state is SessionState.SESSION_VISIBLE


def test_a_different_teardown_is_not_converted_to_completed():
    """Une fin pour une autre cause garde son motif : jamais ``COMPLETED``."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())

    commands = handle(
        coordinator, LlmFailed(cause=LlmFailureCause.ERROR, connection_id="s1")
    )

    assert coordinator.state is SessionState.IDLE
    closed = [trace for trace in traces(commands) if trace.kind == "SessionClosed"]
    assert closed[0].payload["reason"] == "ERROR"


def test_stale_connection_timer_after_completed_close_is_inert():
    """Une échéance périmée ne peut jamais toucher une Session déjà fermée."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)

    stale = handle(coordinator, TimerFired(connection_id="s1"))

    assert coordinator.state is SessionState.IDLE
    assert not [c for c in stale if isinstance(c, CloseLiveConnection)]
    assert not [c for c in stale if isinstance(c, AnnounceSessionEnded)]
    assert traces(stale)[0].level == "debug"
