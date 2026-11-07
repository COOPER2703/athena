"""Tool du noyau ``end_session`` et clôture en attente (#130).

On n'observe que le comportement externe du ``Coordinator`` : les Commandes
émises, l'état de la Session et les Traces. Aucun timer réel : l'échéance de la
grâce est injectée par ``TimerFired`` sur la clé de la Connexion, exactement ce
que fera l'horloge. Le Tool est exercé à travers un vrai ``ToolRegistry``.
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
    ShutdownRequested,
    StartIdleTimer,
    TimerFired,
    ToolRegistry,
)
from server.core.coordinator import (
    END_SESSION_CEILING_TIMEOUT,
    END_SESSION_GRACE_TIMEOUT,
    IDLE_TIMEOUT,
    end_session_ceiling_timer_id,
)
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
    """Le modèle décide de la fin : « non c'est bon » suffit, sans formule explicite."""
    registry = ToolRegistry()
    register_session_tools(registry)
    spec = next(spec for spec in registry.specs() if spec.name == END_SESSION)

    description = spec.description.lower()
    assert "c'est bon" in description
    assert "doute" in description


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
        StartIdleTimer(
            connection_id=end_session_ceiling_timer_id("s1"),
            timeout=END_SESSION_CEILING_TIMEOUT,
        ),
    ]
    assert END_SESSION_GRACE_TIMEOUT == 3.0
    assert END_SESSION_CEILING_TIMEOUT == 10.0
    # La clé du plafond ne peut jamais se confondre avec celle de la Connexion
    # ni avec celle de la fenêtre d'Événements.
    assert end_session_ceiling_timer_id("s1") not in ("s1", "event-window")
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
        CancelIdleTimer(connection_id=end_session_ceiling_timer_id("s1")),
        CloseLiveConnection(connection_id="s1"),
        AnnounceSessionEnded(
            client_id="c1", reason=SessionEndReason.COMPLETED
        ),
    ]
    assert coordinator.state is SessionState.IDLE
    closed = [trace for trace in traces(commands) if trace.kind == "SessionClosed"]
    assert closed[0].payload["reason"] == "COMPLETED"


def test_ceiling_expiry_cancels_the_close_and_rearms_the_idle_timeout():
    """À T+10 s, le modèle parle encore : on renonce à fermer, la Session vit."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)
    ceiling = end_session_ceiling_timer_id("s1")

    commands = handle(coordinator, TimerFired(connection_id=ceiling))

    assert effects(commands) == [
        CancelIdleTimer(connection_id=ceiling),
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
    ]
    assert coordinator.state is SessionState.SESSION_VISIBLE
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]
    assert not [c for c in commands if isinstance(c, AnnounceSessionEnded)]
    canceled = [
        trace for trace in traces(commands) if trace.kind == "SessionEndCanceled"
    ]
    assert len(canceled) == 1
    assert canceled[0].level == "info"
    assert canceled[0].payload["reason"] == "COMPLETED"
    assert canceled[0].payload["cause"] == "audio_overrun"
    assert canceled[0].payload["connection_id"] == "s1"
    assert canceled[0].payload["session_id"] == "s1"


def test_barge_in_during_pending_cancels_the_close_and_keeps_the_session():
    """Un tap annule la clôture, réarme 30 s et suit le barge-in ordinaire."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)
    ceiling = end_session_ceiling_timer_id("s1")

    commands = handle(coordinator, ClientInterrupt(client_id="c1"))

    assert effects(commands) == [
        CancelIdleTimer(connection_id=ceiling),
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
    ]
    assert coordinator.state is SessionState.SESSION_VISIBLE
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]
    assert not [c for c in commands if isinstance(c, AnnounceSessionEnded)]
    assert [trace.kind for trace in traces(commands)] == [
        "SessionEndCanceled",
        "barge_in",
    ]
    canceled = traces(commands)[0]
    assert canceled.payload["cause"] == "barge_in"
    assert canceled.payload["reason"] == "COMPLETED"
    assert canceled.payload["connection_id"] == "s1"
    assert canceled.payload["session_id"] == "s1"


def test_barge_in_during_pending_in_live_active_also_cancels_the_close():
    """La clôture peut être annulée avant même que la Session ne soit visible."""
    coordinator = _coordinator()
    drive(coordinator, [SessionRequested(client_id="c1"), LlmOpened()])
    _call(coordinator)
    assert coordinator.state is SessionState.LIVE_ACTIVE

    commands = handle(coordinator, ClientInterrupt(client_id="c1"))

    assert effects(commands) == [
        CancelIdleTimer(connection_id=end_session_ceiling_timer_id("s1")),
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
    ]
    assert coordinator.state is SessionState.LIVE_ACTIVE
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]
    assert not [c for c in commands if isinstance(c, AnnounceSessionEnded)]
    assert [trace.kind for trace in traces(commands)] == [
        "SessionEndCanceled",
        "ClientInterrupt",
    ]


def test_end_session_after_barge_in_cancellation_reopens_the_pending_close():
    """Après un barge-in, un nouvel ``end_session`` rouvre grâce et plafond."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator, tool_id="t1")
    handle(coordinator, ClientInterrupt(client_id="c1"))

    commands = _call(coordinator, tool_id="t2")

    assert _start_timers(commands) == [
        StartIdleTimer(
            connection_id="s1", timeout=END_SESSION_GRACE_TIMEOUT
        ),
        StartIdleTimer(
            connection_id=end_session_ceiling_timer_id("s1"),
            timeout=END_SESSION_CEILING_TIMEOUT,
        ),
    ]
    assert coordinator.state is SessionState.SESSION_VISIBLE
    handle(coordinator, TimerFired(connection_id="s1"))
    assert coordinator.state is SessionState.IDLE


def test_user_transcription_during_pending_is_journaled_without_rearming_or_canceling():
    """Une interjection orale ne sauve pas la Session, mais reste journalisée."""
    memory = FakeMemory(
        hits=[MemoryHit(text="bleu", distance=0.1, source="journal")]
    )
    coordinator = _coordinator(memory=memory)
    drive(coordinator, visible_session())
    _call(coordinator)

    commands = handle(
        coordinator,
        LlmTranscription(text="encore une question", role=Role.USER),
    )

    assert _start_timers(commands) == []
    assert not [c for c in commands if isinstance(c, CloseLiveConnection)]
    assert not [c for c in commands if isinstance(c, AnnounceSessionEnded)]
    assert not [
        trace for trace in traces(commands) if trace.kind == "SessionEndCanceled"
    ]
    assert memory.turns == [("alice", "user", "encore une question")]
    assert memory.queries == [("alice", "encore une question")]
    recalled = [c for c in commands if isinstance(c, SendMemoryToLlm)]
    assert len(recalled) == 1
    assert "bleu" in recalled[0].text
    assert coordinator.state is SessionState.SESSION_VISIBLE
    # La clôture reste en attente : la grâce armée referme toujours la Session.
    handle(coordinator, TimerFired(connection_id="s1"))
    assert coordinator.state is SessionState.IDLE


def test_background_transcription_does_not_extend_the_pending_close():
    """Plusieurs tours de fond n'arment aucun timer : la grâce n'est pas relancée."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)

    for text in ("bruit", "conversation", "de fond"):
        commands = handle(
            coordinator, LlmTranscription(text=text, role=Role.USER)
        )
        assert _start_timers(commands) == []

    commands = handle(coordinator, TimerFired(connection_id="s1"))
    assert effects(commands) == [
        CancelIdleTimer(connection_id="s1"),
        CancelIdleTimer(connection_id=end_session_ceiling_timer_id("s1")),
        CloseLiveConnection(connection_id="s1"),
        AnnounceSessionEnded(
            client_id="c1", reason=SessionEndReason.COMPLETED
        ),
    ]
    assert coordinator.state is SessionState.IDLE


def test_end_session_after_ceiling_cancellation_reopens_the_pending_close():
    """Après annulation, la Session est normale : un nouvel appel rouvre la clôture."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator, tool_id="t1")
    handle(coordinator, TimerFired(connection_id=end_session_ceiling_timer_id("s1")))

    commands = _call(coordinator, tool_id="t2")

    assert _start_timers(commands) == [
        StartIdleTimer(
            connection_id="s1", timeout=END_SESSION_GRACE_TIMEOUT
        ),
        StartIdleTimer(
            connection_id=end_session_ceiling_timer_id("s1"),
            timeout=END_SESSION_CEILING_TIMEOUT,
        ),
    ]
    assert coordinator.state is SessionState.SESSION_VISIBLE
    # La grâce referme de nouveau, cette fois par le chemin COMPLETED.
    handle(coordinator, TimerFired(connection_id="s1"))
    assert coordinator.state is SessionState.IDLE


def test_another_teardown_cancels_the_ceiling_and_ignores_it_afterwards():
    """Un arrêt pendant la clôture annule le plafond ; l'échéance tardive est inerte."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)
    ceiling = end_session_ceiling_timer_id("s1")

    commands = handle(coordinator, ShutdownRequested())

    assert CancelIdleTimer(connection_id=ceiling) in effects(commands)
    assert coordinator.state is SessionState.IDLE
    stale = handle(coordinator, TimerFired(connection_id=ceiling))
    assert coordinator.state is SessionState.IDLE
    assert not [c for c in stale if isinstance(c, CloseLiveConnection)]
    assert not [c for c in stale if isinstance(c, AnnounceSessionEnded)]
    assert traces(stale)[0].level == "debug"


def test_new_session_after_completed_close_ignores_the_stale_ceiling():
    """Une clé de plafond périmée ne touche jamais la Session suivante."""
    coordinator = _coordinator()
    drive(coordinator, visible_session())
    _call(coordinator)
    handle(coordinator, TimerFired(connection_id=end_session_ceiling_timer_id("s1")))
    drive(
        coordinator,
        [SessionRequested(client_id="c2"), LlmOpened(), LlmAudio(data=b"bonjour")],
    )
    assert coordinator.connection_id == "s2"

    stale = handle(
        coordinator,
        TimerFired(connection_id=end_session_ceiling_timer_id("s1")),
    )

    assert coordinator.state is SessionState.SESSION_VISIBLE
    assert coordinator.connection_id == "s2"
    assert not [c for c in stale if isinstance(c, CloseLiveConnection)]
    assert not [c for c in stale if isinstance(c, AnnounceSessionEnded)]


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


def test_end_session_on_a_judging_connection_is_refused_and_arms_no_timer():
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
    """Une Annonce visible reste sans Tools : ``end_session`` n'y ouvre aucune clôture."""
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
