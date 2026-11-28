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
    PLANNED_REMINDER_TRIGGER,
    ClientRegistered,
    Coordinator,
    EventReceived,
    LlmAudio,
    LlmClosed,
    LlmOpened,
    OccurrenceDropReason,
    OccurrenceReceived,
    OpenLiveConnection,
    SendAnnouncementToLlm,
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
    assert command.text == PLANNED_REMINDER_TRIGGER
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
    assert PLANNED_REMINDER_TRIGGER in command.text
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
    assert command.text == PLANNED_REMINDER_TRIGGER
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
    assert command.text == PLANNED_REMINDER_TRIGGER
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
    assert command.text == PLANNED_REMINDER_TRIGGER
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
    assert command.text == PLANNED_REMINDER_TRIGGER
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
    assert announcement(commands).text == PLANNED_REMINDER_TRIGGER


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
    assert command.text == PLANNED_REMINDER_TRIGGER
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
