"""Couture pure de la fenêtre de regroupement des Événements (ADR-0007, #39).

On n'observe que le comportement externe de ``Coordinator.handle`` : une liste
d'Événements en entrée, une liste de Commandes en sortie. Aucune I/O, aucun
temps réel : la fenêtre est close en injectant ``TimerFired`` sur la **clé
réservée** que le noyau a armée, exactement ce que fera l'horloge à l'échéance.
"""

from __future__ import annotations

import pytest

from conftest import (
    batch,
    close_window,
    drive,
    effects,
    handle,
    opened,
    traces,
    visible_session,
    window_timer,
)
from server.core import (
    EVENT_WINDOW_TIMER_ID,
    EVENT_WINDOW_TIMEOUT,
    IDLE_TIMEOUT,
    JUDGE_TIMEOUT,
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    ClientAudio,
    ClientDisconnected,
    ClientRegistered,
    CloseLiveConnection,
    Coordinator,
    EventDropReason,
    EventReceived,
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
    LlmTranscription,
    OpenLiveConnection,
    Role,
    SendAudioToClient,
    SendAudioToLlm,
    SendJudgeBatchToLlm,
    SendMemoryToLlm,
    SessionRequested,
    SessionState,
    ShutdownRequested,
    StartIdleTimer,
    TimerFired,
)


class FakeMemory:
    def __init__(self, context: str = "") -> None:
        self.context = context
        self.users: list[str] = []
        self.turns: list[tuple[str, str]] = []

    async def session_context(self, user_id: str) -> str:
        self.users.append(user_id)
        return self.context

    async def pinned_facts(self, user_id: str) -> list[str]:
        return []

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        self.turns.append((role, text))

    async def recall(self, user_id: str, query: str) -> list:
        return []


def test_first_event_at_repos_arms_the_window_and_opens_no_connection():
    coordinator = Coordinator(judge_prompt="JUGE")

    commands = handle(coordinator, EventReceived(source="matrix", content="porte"))

    assert effects(commands) == [
        StartIdleTimer(
            connection_id=EVENT_WINDOW_TIMER_ID, timeout=EVENT_WINDOW_TIMEOUT
        )
    ]
    assert coordinator.state is SessionState.IDLE


def test_later_events_do_not_rearm_the_window_nor_open_a_connection():
    coordinator = Coordinator(judge_prompt="JUGE")
    handle(coordinator, EventReceived(source="a", content="un"))

    commands = handle(coordinator, EventReceived(source="b", content="deux"))

    # Fenêtre fixe : pas de nouvel armement, pas de Connexion Live.
    assert effects(commands) == []
    assert coordinator.state is SessionState.IDLE


def test_window_deadline_judges_the_whole_batch_in_one_turn_tagged_by_source():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte ouverte"),
            EventReceived(source="door", content="porte d'entrée"),
            EventReceived(source="matrix", content="colis"),
        ],
    )

    commands = close_window(coordinator)

    assert effects(commands) == [
        OpenLiveConnection(connection_id="s1", instruction="JUGE", tools=()),
        StartIdleTimer(connection_id="s1", timeout=JUDGE_TIMEOUT),
        SendJudgeBatchToLlm(
            connection_id="s1",
            text=(
                "[ÉVÉNEMENTS]\n"
                "- [matrix] porte ouverte\n"
                "- [door] porte d'entrée\n"
                "- [matrix] colis"
            ),
        ),
    ]
    assert coordinator.state is SessionState.LIVE_CONNECTING


def test_isolated_event_is_judged_when_the_window_elapses():
    coordinator = Coordinator(judge_prompt="JUGE")
    handle(coordinator, EventReceived(source="matrix", content="seul"))

    commands = close_window(coordinator)

    assert opened(commands).connection_id == "s1"
    assert batch(commands).text == "[ÉVÉNEMENTS]\n- [matrix] seul"


def test_judgment_uses_the_dedicated_prompt_without_any_memory_recall():
    memory = FakeMemory(context="PROFIL")
    coordinator = Coordinator(memory=memory, user_id="alice", judge_prompt="JUGE")
    handle(coordinator, EventReceived(source="matrix", content="x"))

    commands = close_window(coordinator)

    assert opened(commands).instruction == "JUGE"
    assert memory.users == []
    assert SendMemoryToLlm not in [type(c) for c in commands]


def test_event_during_a_visible_session_is_queued_and_never_injected():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, visible_session())

    commands = handle(coordinator, EventReceived(source="matrix", content="porte"))

    assert effects(commands) == []
    assert coordinator.state is SessionState.SESSION_VISIBLE


def test_events_received_during_a_session_open_the_window_at_repos_in_order():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, visible_session())
    handle(coordinator, EventReceived(source="a", content="premier"))
    handle(coordinator, EventReceived(source="b", content="second"))

    # Retour au Repos : la fenêtre s'arme, aucun jugement immédiat.
    commands = handle(coordinator, LlmClosed(connection_id="s1"))

    assert OpenLiveConnection not in [type(c) for c in commands]
    assert (
        StartIdleTimer(
            connection_id=EVENT_WINDOW_TIMER_ID, timeout=EVENT_WINDOW_TIMEOUT
        )
        in effects(commands)
    )

    commands = close_window(coordinator)

    assert opened(commands).connection_id == "s2"
    assert batch(commands).text == "[ÉVÉNEMENTS]\n- [a] premier\n- [b] second"


def test_speaking_judgment_announces_an_auto_session_to_the_registered_client():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, [ClientRegistered(client_id="c9")])
    drive(
        coordinator,
        [EventReceived(source="matrix", content="porte"), window_timer(), LlmOpened()],
    )

    commands = handle(coordinator, LlmAudio(data=b"parle"))

    assert effects(commands) == [
        StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT),
        AnnounceSessionStarted(client_id="c9", auto=True),
        SendAudioToClient(client_id="c9", data=b"parle"),
    ]
    assert coordinator.state is SessionState.SESSION_VISIBLE


def test_assistant_transcription_from_a_judgment_rearms_the_session_idle_timeout():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [
            ClientRegistered(client_id="c9"),
            EventReceived(source="matrix", content="x"),
            window_timer(),
            LlmOpened(),
        ],
    )

    commands = handle(
        coordinator, LlmTranscription(role=Role.ASSISTANT, text="bonjour")
    )

    eff = effects(commands)
    assert AnnounceSessionStarted(client_id="c9", auto=True) in eff
    assert StartIdleTimer(connection_id="s1", timeout=IDLE_TIMEOUT) in eff


def test_silent_judgment_times_out_without_any_session_or_announcement():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [
            ClientRegistered(client_id="c9"),
            EventReceived(source="matrix", content="x"),
            window_timer(),
            LlmOpened(),
        ],
    )

    commands = handle(coordinator, TimerFired(connection_id="s1"))

    assert effects(commands) == [
        CancelIdleTimer(connection_id="s1"),
        CloseLiveConnection(connection_id="s1"),
    ]
    assert AnnounceSessionStarted not in [type(c) for c in commands]
    assert AnnounceSessionEnded not in [type(c) for c in commands]
    assert coordinator.state is SessionState.IDLE


def test_events_received_while_judging_are_queued_and_judged_after_the_silence():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [EventReceived(source="m", content="un"), window_timer(), LlmOpened()],
    )

    commands = handle(coordinator, EventReceived(source="m", content="deux"))

    assert effects(commands) == []
    assert coordinator.state is SessionState.LIVE_ACTIVE

    # Silence du jugement : retour au Repos, la File rouvre une fenêtre.
    commands = handle(coordinator, TimerFired(connection_id="s1"))
    assert OpenLiveConnection not in [type(c) for c in commands]
    assert (
        StartIdleTimer(
            connection_id=EVENT_WINDOW_TIMER_ID, timeout=EVENT_WINDOW_TIMEOUT
        )
        in effects(commands)
    )

    commands = close_window(coordinator)
    assert batch(commands).text == "[ÉVÉNEMENTS]\n- [m] deux"


def test_adapter_audio_is_processed_immediately_while_judging():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [EventReceived(source="m", content="x"), window_timer(), LlmOpened()],
    )

    commands = handle(coordinator, ClientAudio(data=b"mic"))

    assert SendAudioToLlm(connection_id="s1", data=b"mic") in effects(commands)


def test_event_arriving_during_judgment_does_not_open_a_second_connection():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [EventReceived(source="m", content="un"), window_timer(), LlmOpened()],
    )

    commands = handle(coordinator, EventReceived(source="m", content="deux"))

    assert OpenLiveConnection not in [type(c) for c in commands]


def test_lifecycle_entry_during_the_window_is_processed_immediately():
    coordinator = Coordinator(judge_prompt="JUGE")
    handle(coordinator, EventReceived(source="m", content="x"))

    commands = handle(coordinator, SessionRequested(client_id="c1"))

    assert opened(commands).connection_id == "s1"
    assert coordinator.state is SessionState.LIVE_CONNECTING


def test_window_timer_after_the_user_took_over_is_ignored():
    coordinator = Coordinator(judge_prompt="JUGE")
    handle(coordinator, EventReceived(source="m", content="x"))
    handle(coordinator, SessionRequested(client_id="c1"))

    commands = handle(coordinator, window_timer())

    assert effects(commands) == []
    assert coordinator.state is SessionState.LIVE_CONNECTING


def test_opening_the_window_is_traced():
    coordinator = Coordinator(judge_prompt="JUGE")

    commands = handle(coordinator, EventReceived(source="matrix", content="x"))

    assert any(t.kind == "EventWindowOpened" for t in traces(commands))


def test_shutdown_drops_the_queued_events_without_opening_a_judgment():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, visible_session())
    handle(coordinator, EventReceived(source="m", content="x"))

    commands = handle(coordinator, ShutdownRequested())

    assert OpenLiveConnection not in [type(c) for c in commands]
    assert SendJudgeBatchToLlm not in [type(c) for c in commands]
    assert coordinator.state is SessionState.IDLE
    assert any(t.kind == "EventQueueDropped" for t in traces(commands))


# ---------------------------------------------------------------------------
# Priorité : l'utilisateur gagne toujours (ticket #41, ADR-0007). Un
# ``SessionRequested`` (Wake word ou tap) prime sur les automatismes : pendant
# une fenêtre ouverte il l'annule en laissant la File intacte ; pendant un
# jugement en vol il préempte la Connexion Live et remet les faits du lot en
# tête de la File, pour qu'ils soient re-jugés plus tard — aucune perte.
# ---------------------------------------------------------------------------


def test_session_requested_during_the_window_cancels_it_and_keeps_the_events():
    coordinator = Coordinator(judge_prompt="JUGE")
    handle(coordinator, EventReceived(source="matrix", content="porte"))

    commands = handle(coordinator, SessionRequested(client_id="c1"))

    eff = effects(commands)
    # La fenêtre est annulée sur sa clé réservée...
    assert CancelIdleTimer(connection_id=EVENT_WINDOW_TIMER_ID) in eff
    # ...et aucune Connexion de jugement ne s'ouvre : la seule ouverte est la
    # Session utilisateur (instruction de conversation vide ici, pas le prompt
    # de jugement).
    assert SendJudgeBatchToLlm not in [type(c) for c in commands]
    assert [c.instruction for c in eff if isinstance(c, OpenLiveConnection)] == [""]
    assert any(t.kind == "EventWindowCanceled" for t in traces(commands))
    assert coordinator.state is SessionState.LIVE_CONNECTING

    # La Session utilisateur se déroule puis se termine ; au Repos l'Événement
    # toujours en File est jugé, jamais perdu.
    drive(coordinator, [LlmOpened(), LlmAudio(data=b"hi")])
    commands = handle(coordinator, LlmClosed(connection_id="s1"))

    assert (
        StartIdleTimer(
            connection_id=EVENT_WINDOW_TIMER_ID, timeout=EVENT_WINDOW_TIMEOUT
        )
        in effects(commands)
    )

    commands = close_window(coordinator)

    assert batch(commands).text == "[ÉVÉNEMENTS]\n- [matrix] porte"


def test_stale_window_timer_after_cancellation_is_a_no_op():
    coordinator = Coordinator(judge_prompt="JUGE")
    handle(coordinator, EventReceived(source="matrix", content="porte"))
    handle(coordinator, SessionRequested(client_id="c1"))

    commands = handle(coordinator, window_timer())

    assert effects(commands) == []
    assert SendJudgeBatchToLlm not in [type(c) for c in commands]
    assert coordinator.state is SessionState.LIVE_CONNECTING


def test_session_requested_during_a_judgment_preempts_and_requeues_the_facts():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            window_timer(),
            LlmOpened(),
        ],
    )
    # Un Événement arrive pendant le jugement : il rejoint la File.
    handle(coordinator, EventReceived(source="door", content="colis"))

    commands = handle(coordinator, SessionRequested(client_id="c1"))

    eff = effects(commands)
    # Même chemin de fin que le remplacement existant.
    assert CancelIdleTimer(connection_id="s1") in eff
    assert CloseLiveConnection(connection_id="s1") in eff
    assert [c.connection_id for c in eff if isinstance(c, OpenLiveConnection)] == [
        "s2"
    ]
    assert coordinator.state is SessionState.LIVE_CONNECTING

    # La Session utilisateur se déroule puis se termine.
    drive(coordinator, [LlmOpened(), LlmAudio(data=b"hi")])
    commands = handle(coordinator, LlmClosed(connection_id="s2"))
    commands = close_window(coordinator)

    # Les faits préemptés sont re-jugés, en tête et dans leur ordre d'origine,
    # avant l'Événement arrivé pendant le jugement.
    assert batch(commands).text == (
        "[ÉVÉNEMENTS]\n- [matrix] porte\n- [door] colis"
    )


def test_stale_judgment_timer_after_preemption_does_not_touch_the_user_session():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            window_timer(),
            LlmOpened(),
        ],
    )
    handle(coordinator, SessionRequested(client_id="c1"))

    commands = handle(coordinator, TimerFired(connection_id="s1"))

    assert effects(commands) == []
    assert coordinator.state is SessionState.LIVE_CONNECTING


# ---------------------------------------------------------------------------
# Échec d'un jugement en vol (#40/#41) : le lot est sorti de la File, donc sa
# perte n'est visible que par la trace. Toute fin anormale le compte un fait par
# ``EventQueueDropped`` (``judgment_aborted``). La préemption (#41) fait
# exception : le lot est remis en File avant la fin, il n'est donc jamais tracé
# comme perdu.
# ---------------------------------------------------------------------------


def judging_connection(source="matrix", content="porte"):
    """Connexion de jugement en vol : le lot a quitté la File, pas encore Annoncé."""
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [
            ClientRegistered(client_id="c9"),
            EventReceived(source=source, content=content),
            window_timer(),
            LlmOpened(),
        ],
    )
    return coordinator


def judgment_drops(commands):
    return [t for t in traces(commands) if t.kind == "EventQueueDropped"]


@pytest.mark.parametrize(
    "abort",
    [
        LlmFailed(cause=LlmFailureCause.ERROR, connection_id="s1"),
        LlmClosed(connection_id="s1"),
        ClientDisconnected(client_id="c9"),
        ShutdownRequested(),
    ],
    ids=["llm_failed", "llm_closed", "client_disconnected", "shutdown"],
)
def test_an_aborted_judgment_traces_each_in_flight_fact_as_dropped(abort):
    coordinator = judging_connection()

    commands = handle(coordinator, abort)

    dropped = judgment_drops(commands)
    assert len(dropped) == 1
    assert dropped[0].payload["reason"] == EventDropReason.JUDGMENT_ABORTED
    assert dropped[0].payload["source"] == "matrix"
    assert coordinator.state is SessionState.IDLE


def test_an_aborted_judgment_traces_every_fact_of_the_batch():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            EventReceived(source="door", content="colis"),
            window_timer(),
            LlmOpened(),
        ],
    )

    commands = handle(coordinator, LlmClosed(connection_id="s1"))

    dropped = judgment_drops(commands)
    assert len(dropped) == 2
    assert {t.payload["source"] for t in dropped} == {"matrix", "door"}


def test_a_preempted_judgment_traces_no_drop_for_the_requeued_facts():
    coordinator = judging_connection()

    commands = handle(coordinator, SessionRequested(client_id="c1"))

    assert judgment_drops(commands) == []


# ---------------------------------------------------------------------------
# Journal du fait annoncé (ticket #42, ADR-0007) : un Événement annoncé entre
# au Journal en rôle ``event`` — le fait et la parole d'annonce — tandis qu'un
# Événement tu n'écrit rien.
# ---------------------------------------------------------------------------


def judging(memory, *, source="matrix", content="porte"):
    """Amène le noyau jusqu'au jugement d'un Événement, Connexion Live ouverte."""
    coordinator = Coordinator(memory=memory, user_id="alice", judge_prompt="JUGE")
    drive(
        coordinator,
        [
            ClientRegistered(client_id="c9"),
            EventReceived(source=source, content=content),
            window_timer(),
            LlmOpened(),
        ],
    )
    return coordinator


def test_announced_event_journals_the_fact_and_the_announcement_under_event():
    memory = FakeMemory()

    coordinator = judging(memory)
    handle(
        coordinator,
        LlmTranscription(role=Role.ASSISTANT, text="Porte d'entrée ouverte"),
    )

    assert memory.turns == [
        ("event", "[matrix] porte"),
        ("event", "Porte d'entrée ouverte"),
    ]


def test_judgment_audio_journals_the_fact_under_event_without_assistant_role():
    memory = FakeMemory()

    coordinator = judging(memory)
    commands = handle(coordinator, LlmAudio(data=b"parle"))

    assert AnnounceSessionStarted(client_id="c9", auto=True) in effects(commands)
    assert memory.turns == [("event", "[matrix] porte")]


def test_silent_judgment_writes_nothing_to_the_journal():
    memory = FakeMemory()

    coordinator = judging(memory)
    handle(coordinator, TimerFired(connection_id="s1"))

    assert memory.turns == []


def test_announcement_scope_ends_on_the_first_user_turn():
    memory = FakeMemory()

    coordinator = judging(memory)
    handle(coordinator, LlmTranscription(role=Role.ASSISTANT, text="Porte ouverte"))
    handle(coordinator, LlmTranscription(role=Role.USER, text="merci"))
    handle(coordinator, LlmTranscription(role=Role.ASSISTANT, text="je t'en prie"))

    assert memory.turns == [
        ("event", "[matrix] porte"),
        ("event", "Porte ouverte"),
        ("user", "merci"),
        ("assistant", "je t'en prie"),
    ]


def test_a_normal_session_keeps_journaling_user_and_assistant():
    memory = FakeMemory()
    coordinator = Coordinator(memory=memory, user_id="alice", system_prompt="S")
    drive(coordinator, visible_session())

    handle(coordinator, LlmTranscription(role=Role.USER, text="salut"))
    handle(coordinator, LlmTranscription(role=Role.ASSISTANT, text="bonjour"))

    assert memory.turns == [("user", "salut"), ("assistant", "bonjour")]
