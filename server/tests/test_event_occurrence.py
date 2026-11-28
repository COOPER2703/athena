"""Occurrences planifiées au Repos : annonce sans jugement (ADR-0028, #195).

Couture pure ``Coordinator.handle`` : une Occurrence entre dans la **même** File
d'attente qu'un Événement, elle n'est jamais dédupliquée, et au Repos elle ouvre
directement l'Annonce — le juge ne voit que les Événements ordinaires d'un lot
mixte. Le contenu d'une Planification est un **fait** : il part sous
``[ÉVÉNEMENTS]``, jamais comme la phrase à prononcer.
"""

from __future__ import annotations

from harness import (
    Presence,
    announcement,
    close_window,
    drive,
    effects,
    handle,
    opened,
    traces,
    visible_session,
    window_timer,
)
from testkit import FakeJudge
from server.core import (
    EVENT_MAX_CHARS,
    EVENT_QUEUE_MAX,
    OCCURRENCE_TRIGGER,
    ClientDisconnected,
    ClientRegistered,
    Coordinator,
    EventReceived,
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
    OccurrenceDropReason,
    OccurrenceReceived,
    OpenLiveConnection,
    SendAnnouncementToLlm,
    SessionRequested,
    SessionState,
    ShutdownRequested,
    StartIdleTimer,
    TimerFired,
)


def at_rest(*client_ids: str, judge=None) -> tuple[Coordinator, FakeJudge | None, Presence]:
    """Noyau au Repos, présence simulée, juge optionnel."""
    presence = Presence(*client_ids)
    coordinator = Coordinator(
        announce_prompt="JUGE",
        judge=judge,
        connected_clients=presence,
    )
    return coordinator, judge, presence


def occurrence_drops(commands, reason):
    return [
        trace
        for trace in traces(commands)
        if trace.kind == "OccurrenceDropped"
        and trace.payload.get("reason") == reason
    ]


def occurrence_deferrals(commands):
    return [
        trace
        for trace in traces(commands)
        if trace.kind == "OccurrenceDeferred"
        and trace.payload.get("reason") == "no_client"
    ]


def test_an_occurrence_at_rest_is_announced_without_calling_the_judge():
    judge = FakeJudge(message="Phrase du juge qui ne doit pas servir")
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    handle(
        coordinator,
        OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
    )
    commands = close_window(coordinator)

    assert judge.calls == []
    command = announcement(commands)
    assert command.text == OCCURRENCE_TRIGGER
    assert "[planificateur] appeler le plombier" in command.facts
    # Le contenu est un fait : il n'est jamais la phrase prononcée.
    assert "appeler le plombier" not in command.text


def test_two_occurrences_with_the_same_content_are_both_announced():
    judge = FakeJudge()
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    drive(
        coordinator,
        [
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_2"),
        ],
    )
    commands = close_window(coordinator)

    assert judge.calls == []
    facts = announcement(commands).facts.splitlines()
    assert facts == [
        "[ÉVÉNEMENTS]",
        "- [planificateur] sortir les poubelles",
        "- [planificateur] sortir les poubelles",
    ]
    # Une seule Annonce porte les deux Occurrences, dans l'ordre d'arrivée.
    assert len([c for c in commands if isinstance(c, OpenLiveConnection)]) == 1


def test_a_mixed_lot_shows_the_judge_only_the_ordinary_events():
    judge = FakeJudge(message="La porte est ouverte")
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
        ],
    )
    commands = close_window(coordinator)

    assert judge.calls == [["[matrix] porte"]]
    command = announcement(commands)
    assert command.text.startswith("La porte est ouverte")
    assert OCCURRENCE_TRIGGER in command.text
    assert "[matrix] porte" in command.facts
    assert "[planificateur] sortir les poubelles" in command.facts
    assert len([c for c in commands if isinstance(c, OpenLiveConnection)]) == 1


def test_a_silent_verdict_drops_the_ordinary_event_but_keeps_the_occurrence():
    judge = FakeJudge(announce=False)
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
        ],
    )
    commands = close_window(coordinator)

    assert judge.calls == [["[matrix] porte"]]
    command = announcement(commands)
    assert command.text == OCCURRENCE_TRIGGER
    assert "[planificateur] sortir les poubelles" in command.facts
    assert "[matrix] porte" not in command.facts
    assert "EventJudged" in [trace.kind for trace in traces(commands)]


def test_a_failed_judgment_still_announces_the_occurrence():
    judge = FakeJudge(failed=True)
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
        ],
    )
    commands = close_window(coordinator)

    kinds = [trace.kind for trace in traces(commands)]
    assert "EventJudgmentFailed" in kinds
    command = announcement(commands)
    assert command.text == OCCURRENCE_TRIGGER
    assert "[planificateur] sortir les poubelles" in command.facts
    assert "[matrix] porte" not in command.facts


def test_a_malformed_verdict_still_announces_the_occurrence():
    judge = FakeJudge(message="")
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
        ],
    )
    commands = close_window(coordinator)

    assert "EventJudgmentFailed" in [trace.kind for trace in traces(commands)]
    command = announcement(commands)
    assert command.text == OCCURRENCE_TRIGGER
    assert "[planificateur] sortir les poubelles" in command.facts
    assert "porte" not in command.facts


def test_an_occurrence_is_announced_even_without_any_judge_injected():
    coordinator, _judge, _presence = at_rest("c1")

    handle(
        coordinator,
        OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
    )
    commands = close_window(coordinator)

    command = announcement(commands)
    assert command.text == OCCURRENCE_TRIGGER
    assert "[planificateur] appeler le plombier" in command.facts


def test_an_occurrence_waits_for_the_end_of_a_session_without_interrupting():
    judge = FakeJudge()
    coordinator, judge, _presence = at_rest("c1", judge=judge)
    drive(coordinator, visible_session())

    commands = handle(
        coordinator,
        OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
    )

    assert effects(commands) == []
    assert coordinator.state is SessionState.SESSION_VISIBLE

    commands = handle(coordinator, LlmClosed(connection_id="s1"))

    assert StartIdleTimer in [type(command) for command in commands]
    commands = close_window(coordinator)
    assert judge.calls == []
    assert announcement(commands).text == OCCURRENCE_TRIGGER


def test_an_occurrence_without_a_client_is_deferred_and_announced_to_the_first_one():
    judge = FakeJudge()
    coordinator, judge, presence = at_rest(judge=judge)

    commands = handle(
        coordinator,
        OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
    )

    assert effects(commands) == []
    assert occurrence_deferrals(commands)
    assert coordinator.state is SessionState.IDLE

    presence.connect("c1")
    commands = handle(coordinator, ClientRegistered(client_id="c1"))
    assert StartIdleTimer in [type(command) for command in commands]
    assert coordinator.state is SessionState.IDLE

    commands = close_window(coordinator)
    assert judge.calls == []
    command = announcement(commands)
    assert command.text == OCCURRENCE_TRIGGER
    assert opened(commands).connection_id == coordinator.connection_id


def test_an_oversized_occurrence_is_rejected_on_arrival_and_traced():
    judge = FakeJudge()
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    commands = handle(
        coordinator,
        OccurrenceReceived(
            content="x" * (EVENT_MAX_CHARS + 1), schedule_id="sch_1"
        ),
    )

    assert len(occurrence_drops(commands, OccurrenceDropReason.TOO_LARGE)) == 1
    assert effects(commands) == []


def test_the_occurrence_ceiling_drops_the_oldest_and_traces_it():
    judge = FakeJudge()
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    commands = []
    for index in range(EVENT_QUEUE_MAX + 1):
        commands.extend(
            handle(
                coordinator,
                OccurrenceReceived(content=f"fait-{index}", schedule_id=f"sch_{index}"),
            )
        )

    assert len(occurrence_drops(commands, OccurrenceDropReason.CAPACITY)) == 1

    commands = close_window(coordinator)
    facts = announcement(commands).facts.splitlines()[1:]
    assert len(facts) == EVENT_QUEUE_MAX
    assert facts[0] == "- [planificateur] fait-1"
    assert facts[-1] == f"- [planificateur] fait-{EVENT_QUEUE_MAX}"


def test_shutdown_drops_pending_occurrences_with_their_own_trace():
    coordinator, _judge, _presence = at_rest("c1")

    handle(
        coordinator,
        OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
    )
    commands = handle(coordinator, ShutdownRequested())

    assert len(occurrence_drops(commands, OccurrenceDropReason.SHUTDOWN)) == 1
    assert SendAnnouncementToLlm not in [type(command) for command in commands]


def test_a_silent_announcement_is_traced_once_without_retry():
    judge = FakeJudge()
    coordinator, judge, _presence = at_rest("c1", judge=judge)
    drive(
        coordinator,
        [
            OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
            window_timer(),
            LlmOpened(),
        ],
    )

    commands = handle(coordinator, TimerFired(connection_id="s1"))

    silent = [trace for trace in traces(commands) if trace.kind == "AnnouncementSilent"]
    assert len(silent) == 1
    assert coordinator.state is SessionState.IDLE


class FakeMemory:
    """Journal minimal : seuls les tours écrits sont observés."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []

    async def session_context(self, user_id: str) -> str:
        return ""

    async def pinned_facts(self, user_id: str) -> list[str]:
        return []

    async def append_turn(
        self, user_id: str, role: str, text: str, origin: str = ""
    ) -> None:
        self.turns.append((role, text))

    async def recall(self, user_id: str, query: str) -> list:
        return []


def test_an_occurrence_is_journaled_only_once_the_announcement_becomes_visible():
    memory = FakeMemory()
    coordinator = Coordinator(
        memory=memory,
        user_id="alice",
        announce_prompt="JUGE",
        judge=FakeJudge(),
        connected_clients=lambda: ["c1"],
    )
    drive(
        coordinator,
        [
            OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
            window_timer(),
        ],
    )

    # Connexion ouverte mais jamais visible : rien n'entre au Journal.
    handle(coordinator, LlmOpened())
    assert memory.turns == []

    drive(coordinator, [LlmAudio(data=b"rappel")])

    assert ("event", "[planificateur] appeler le plombier") in memory.turns


def test_a_silenced_ordinary_event_never_enters_the_journal_of_a_mixed_lot():
    memory = FakeMemory()
    coordinator = Coordinator(
        memory=memory,
        user_id="alice",
        announce_prompt="JUGE",
        judge=FakeJudge(announce=False),
        connected_clients=lambda: ["c1"],
    )
    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
            window_timer(),
            LlmOpened(),
            LlmAudio(data=b"rappel"),
        ],
    )

    facts = [text for role, text in memory.turns if role == "event"]
    assert "[planificateur] sortir les poubelles" in facts
    assert "[matrix] porte" not in facts


# ---------------------------------------------------------------------------
# Abandon d'une Annonce avant visibilité (#198) : une Occurrence est un
# engagement de l'utilisateur, jamais une perte silencieuse. Si la Connexion
# d'Annonce échoue avant d'avoir parlé, le rappel retourne en tête de File et
# l'Annonce suivante le journalise **une fois** — ni zéro, ni deux.
# ---------------------------------------------------------------------------


def test_an_occurrence_aborted_by_an_llm_failure_is_announced_later_once():
    memory = FakeMemory()
    coordinator = Coordinator(
        memory=memory,
        user_id="alice",
        announce_prompt="JUGE",
        judge=FakeJudge(),
        connected_clients=lambda: ["c1"],
    )
    drive(
        coordinator,
        [
            OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
            window_timer(),
            LlmOpened(),
        ],
    )

    commands = handle(
        coordinator, LlmFailed(cause=LlmFailureCause.ERROR, connection_id="s1")
    )

    assert coordinator.state is SessionState.IDLE
    # L'Occurrence est intacte en File : la fenêtre se réarme pour la relancer.
    assert StartIdleTimer in [type(command) for command in commands]
    assert occurrence_drops(commands, OccurrenceDropReason.JUDGMENT_ABORTED) == []
    assert memory.turns == []

    # Une Annonce ultérieure la prononce, et le Journal la reçoit une fois.
    announcement(close_window(coordinator))
    drive(coordinator, [LlmOpened(), LlmAudio(data=b"rappel")])
    assert coordinator.state is SessionState.SESSION_VISIBLE
    assert memory.turns == [
        ("event", "[planificateur] appeler le plombier"),
    ]


# ---------------------------------------------------------------------------
# Lot mixte (#198) : un seul tour d'Annonce porte les deux faits, le juge ne
# voit que l'Événement ordinaire, et le contenu planifié reste un fait — jamais
# la phrase prononcée.
# ---------------------------------------------------------------------------


def test_a_mixed_lot_announces_one_turn_with_the_judge_phrase_then_the_trigger():
    judge = FakeJudge(message="La porte est ouverte")
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
        ],
    )
    commands = close_window(coordinator)

    assert judge.calls == [["[matrix] porte"]]
    assert len([c for c in commands if isinstance(c, OpenLiveConnection)]) == 1
    command = announcement(commands)
    # Exactement le message du juge, puis la consigne neutre de rappel.
    assert command.text == f"La porte est ouverte\n\n{OCCURRENCE_TRIGGER}"
    # Le contenu planifié n'est jamais lu mot pour mot : c'est un fait.
    assert "poubelles" not in command.text
    assert command.facts.splitlines() == [
        "[ÉVÉNEMENTS]",
        "- [matrix] porte",
        "- [planificateur] sortir les poubelles",
    ]


def test_the_failed_judgment_trace_is_scoped_to_the_ordinary_events():
    judge = FakeJudge(failed=True)
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            EventReceived(source="door", content="colis"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
        ],
    )
    commands = close_window(coordinator)

    assert judge.calls == [["[matrix] porte", "[door] colis"]]
    failed = [
        trace for trace in traces(commands) if trace.kind == "EventJudgmentFailed"
    ]
    # Une seule trace, portant le compte des Événements ordinaires seulement.
    assert len(failed) == 1
    assert failed[0].payload["events"] == 2
    command = announcement(commands)
    assert command.text == OCCURRENCE_TRIGGER
    assert command.facts.splitlines() == [
        "[ÉVÉNEMENTS]",
        "- [planificateur] sortir les poubelles",
    ]


def test_an_occurrence_only_lot_never_consults_the_judge():
    judge = FakeJudge(message="Phrase qui ne doit jamais servir")
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    drive(
        coordinator,
        [
            OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
            OccurrenceReceived(content="point hebdo", schedule_id="sch_2"),
        ],
    )
    commands = close_window(coordinator)

    assert judge.calls == []
    assert announcement(commands).text == OCCURRENCE_TRIGGER


def test_the_journal_receives_each_announced_fact_once_in_arrival_order():
    memory = FakeMemory()
    coordinator = Coordinator(
        memory=memory,
        user_id="alice",
        announce_prompt="JUGE",
        judge=FakeJudge(message="La porte est ouverte"),
        connected_clients=lambda: ["c1"],
    )
    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
            OccurrenceReceived(content="appeler le plombier", schedule_id="sch_2"),
            window_timer(),
            LlmOpened(),
            LlmAudio(data=b"rappel"),
        ],
    )

    assert memory.turns == [
        ("event", "[matrix] porte"),
        ("event", "[planificateur] sortir les poubelles"),
        ("event", "[planificateur] appeler le plombier"),
    ]


# ---------------------------------------------------------------------------
# File d'attente (#198) : l'ordre d'arrivée et le dédoublonnage restent ceux
# des Événements ordinaires. Les Occurrences ne sont jamais dédupliquées, même
# identiques ou identiques au contenu d'un Événement.
# ---------------------------------------------------------------------------


def test_an_ordinary_duplicate_collapses_around_occurrences():
    judge = FakeJudge(message="La porte est ouverte")
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    commands = drive(
        coordinator,
        [
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
            EventReceived(source="matrix", content="porte"),
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="appeler le plombier", schedule_id="sch_2"),
        ],
    )
    duplicates = [
        trace
        for trace in traces(commands)
        if trace.kind == "EventQueueDropped"
        and trace.payload.get("reason") == "duplicate"
    ]
    assert len(duplicates) == 1

    commands = close_window(coordinator)

    # Le juge ne voit que l'unique Événement ordinaire, dans le lot complet.
    assert judge.calls == [["[matrix] porte"]]
    assert announcement(commands).facts.splitlines() == [
        "[ÉVÉNEMENTS]",
        "- [planificateur] sortir les poubelles",
        "- [matrix] porte",
        "- [planificateur] appeler le plombier",
    ]


def test_an_occurrence_and_an_ordinary_event_with_the_same_content_are_both_kept():
    judge = FakeJudge(message="La porte est ouverte")
    coordinator, judge, _presence = at_rest("c1", judge=judge)

    drive(
        coordinator,
        [
            OccurrenceReceived(content="porte", schedule_id="sch_1"),
            EventReceived(source="matrix", content="porte"),
        ],
    )
    commands = close_window(coordinator)

    assert judge.calls == [["[matrix] porte"]]
    assert announcement(commands).facts.splitlines() == [
        "[ÉVÉNEMENTS]",
        "- [planificateur] porte",
        "- [matrix] porte",
    ]


# ---------------------------------------------------------------------------
# Préemption d'une Annonce (#198) : l'utilisateur gagne, le lot revient intact
# devant la File, et il n'est journalisé qu'une fois — à l'Annonce qui parle.
# ---------------------------------------------------------------------------


def test_a_mixed_lot_preempted_by_a_user_session_is_announced_and_journaled_once():
    memory = FakeMemory()
    judge = FakeJudge(message="La porte est ouverte")
    coordinator = Coordinator(
        memory=memory,
        user_id="alice",
        announce_prompt="JUGE",
        judge=judge,
        connected_clients=lambda: ["c1"],
    )
    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
            window_timer(),
            LlmOpened(),
        ],
    )

    commands = handle(coordinator, SessionRequested(client_id="c2"))

    # Le lot préempté n'est pas perdu : aucun fait abandonné, journal vide.
    assert occurrence_drops(commands, OccurrenceDropReason.JUDGMENT_ABORTED) == []
    assert [
        trace
        for trace in traces(commands)
        if trace.kind == "EventQueueDropped"
        and trace.payload.get("reason") == "judgment_aborted"
    ] == []
    assert memory.turns == []

    # L'utilisateur tient sa Session, puis la rend : la File ré-annonce.
    session = coordinator.connection_id
    drive(coordinator, [LlmOpened(), LlmAudio(data=b"bonjour")])
    assert coordinator.state is SessionState.SESSION_VISIBLE
    handle(coordinator, LlmClosed(connection_id=session))
    commands = close_window(coordinator)

    # Le juge revoit l'Événement ordinaire, l'Occurrence reste dans le lot.
    assert judge.calls == [["[matrix] porte"], ["[matrix] porte"]]
    assert announcement(commands).text == (
        f"La porte est ouverte\n\n{OCCURRENCE_TRIGGER}"
    )

    drive(coordinator, [LlmOpened(), LlmAudio(data=b"rappel")])
    assert memory.turns == [
        ("event", "[matrix] porte"),
        ("event", "[planificateur] sortir les poubelles"),
    ]


def test_a_preempted_occurrence_is_requeued_intact_and_traced_once():
    """Une Occurrence interrompue avant visibilité est reportée, jamais perdue."""
    judge = FakeJudge(message="La porte est ouverte")
    coordinator = Coordinator(
        memory=FakeMemory(),
        user_id="alice",
        announce_prompt="JUGE",
        judge=judge,
        connected_clients=lambda: ["c1"],
    )
    drive(
        coordinator,
        [
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
            window_timer(),
            LlmOpened(),
        ],
    )

    commands = handle(coordinator, SessionRequested(client_id="c2"))

    deferred = [
        trace
        for trace in traces(commands)
        if trace.kind == "OccurrenceDeferred"
        and trace.payload.get("reason") == "announcement_aborted"
    ]
    assert [trace.payload["schedule_id"] for trace in deferred] == ["sch_1"]
    assert deferred[0].payload.get("cause") == "preempted"
    # Aucun abandon : l'Occurrence est intacte en tête de File.
    assert occurrence_drops(commands, OccurrenceDropReason.JUDGMENT_ABORTED) == []

    # La Session utilisateur passe, puis la File ré-annonce une seule fois.
    session = coordinator.connection_id
    drive(coordinator, [LlmOpened(), LlmAudio(data=b"bonjour")])
    handle(coordinator, LlmClosed(connection_id=session))
    commands = close_window(coordinator)

    assert announcement(commands).facts.splitlines() == [
        "[ÉVÉNEMENTS]",
        "- [planificateur] sortir les poubelles",
    ]
    assert [trace.kind for trace in traces(commands)].count(
        "OccurrenceDeferred"
    ) == 0


def test_a_mixed_lot_lost_to_an_llm_failure_keeps_only_the_occurrence():
    memory = FakeMemory()
    judge = FakeJudge(message="La porte est ouverte")
    coordinator = Coordinator(
        memory=memory,
        user_id="alice",
        announce_prompt="JUGE",
        judge=judge,
        connected_clients=lambda: ["c1"],
    )
    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
            window_timer(),
            LlmOpened(),
        ],
    )

    commands = handle(
        coordinator, LlmFailed(cause=LlmFailureCause.ERROR, connection_id="s1")
    )

    # L'Événement ordinaire suit la politique existante (perdu, tracé) ;
    # l'Occurrence est reportée, pas abandonnée.
    assert [
        trace
        for trace in traces(commands)
        if trace.kind == "EventQueueDropped"
        and trace.payload.get("reason") == "judgment_aborted"
    ]
    deferred = [
        trace
        for trace in traces(commands)
        if trace.kind == "OccurrenceDeferred"
        and trace.payload.get("reason") == "announcement_aborted"
    ]
    assert [trace.payload["schedule_id"] for trace in deferred] == ["sch_1"]

    # La relance ne porte que l'Occurrence : le juge n'est plus consulté.
    calls_before = list(judge.calls)
    commands = close_window(coordinator)
    assert judge.calls == calls_before
    command = announcement(commands)
    assert command.text == OCCURRENCE_TRIGGER
    assert command.facts.splitlines() == [
        "[ÉVÉNEMENTS]",
        "- [planificateur] sortir les poubelles",
    ]

    drive(coordinator, [LlmOpened(), LlmAudio(data=b"rappel")])
    assert memory.turns == [("event", "[planificateur] sortir les poubelles")]


def test_a_silent_announcement_timeout_does_not_retry_the_occurrence():
    coordinator, _judge, _presence = at_rest("c1")
    drive(
        coordinator,
        [
            OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
            window_timer(),
            LlmOpened(),
        ],
    )

    commands = handle(coordinator, TimerFired(connection_id="s1"))

    # ADR-0028 : une Annonce silencieuse est tracée une fois, sans relance.
    assert [t.kind for t in traces(commands)].count("AnnouncementSilent") == 1
    assert [t.kind for t in traces(commands)].count("OccurrenceDeferred") == 0
    assert len(occurrence_drops(commands, OccurrenceDropReason.JUDGMENT_ABORTED)) == 1
    assert StartIdleTimer not in [type(command) for command in commands]


def test_a_mixed_lot_without_any_judge_announces_the_occurrence_only():
    coordinator, _judge, _presence = at_rest("c1")

    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
        ],
    )
    commands = close_window(coordinator)

    failed = [
        trace for trace in traces(commands) if trace.kind == "EventJudgmentFailed"
    ]
    assert len(failed) == 1
    assert failed[0].payload["events"] == 1
    command = announcement(commands)
    assert command.text == OCCURRENCE_TRIGGER
    assert command.facts.splitlines() == [
        "[ÉVÉNEMENTS]",
        "- [planificateur] sortir les poubelles",
    ]


def test_a_failed_verdict_never_journals_the_silenced_ordinary_event():
    memory = FakeMemory()
    coordinator = Coordinator(
        memory=memory,
        user_id="alice",
        announce_prompt="JUGE",
        judge=FakeJudge(failed=True),
        connected_clients=lambda: ["c1"],
    )
    drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte"),
            OccurrenceReceived(content="sortir les poubelles", schedule_id="sch_1"),
            window_timer(),
            LlmOpened(),
            LlmAudio(data=b"rappel"),
        ],
    )

    assert memory.turns == [
        ("event", "[planificateur] sortir les poubelles"),
    ]


def test_an_occurrence_aborted_by_the_client_leaving_waits_for_the_next_one():
    memory = FakeMemory()
    presence = Presence("c1")
    coordinator = Coordinator(
        memory=memory,
        user_id="alice",
        announce_prompt="JUGE",
        judge=FakeJudge(),
        connected_clients=presence,
    )
    drive(
        coordinator,
        [
            OccurrenceReceived(content="appeler le plombier", schedule_id="sch_1"),
            window_timer(),
            LlmOpened(),
        ],
    )

    presence.disconnect("c1")
    commands = handle(coordinator, ClientDisconnected(client_id="c1"))

    # Le rappel n'est pas perdu ; sans auditeur, aucune fenêtre ne se réarme.
    assert occurrence_drops(commands, OccurrenceDropReason.JUDGMENT_ABORTED) == []
    assert StartIdleTimer not in [type(command) for command in commands]
    assert coordinator.state is SessionState.IDLE

    # Le prochain Client connecté reçoit le rappel, journalisé une fois.
    presence.connect("c2")
    commands = handle(coordinator, ClientRegistered(client_id="c2"))
    assert StartIdleTimer in [type(command) for command in commands]
    announcement(close_window(coordinator))
    drive(coordinator, [LlmOpened(), LlmAudio(data=b"rappel")])
    assert memory.turns == [
        ("event", "[planificateur] appeler le plombier"),
    ]
