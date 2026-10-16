"""Assainissement de la File d'attente d'Événements (ADR-0007, ticket #40).

Couture pure ``Coordinator.handle`` : une liste d'Événements en entrée, une
liste de Commandes en sortie, sans I/O. On vérifie le **plafond** (120), la
**taille maximale** par Événement, la **dédup** exacte ``(source, contenu)``
intra-fenêtre, et l'**éphémérité** à l'arrêt — chaque rejet étant tracé.
"""

from __future__ import annotations

import asyncio

from server.core import (
    EVENT_MAX_CHARS,
    EVENT_QUEUE_MAX,
    EVENT_WINDOW_TIMER_ID,
    Coordinator,
    EventDropReason,
    EventReceived,
    LlmAudio,
    LlmClosed,
    LlmOpened,
    OpenLiveConnection,
    SendJudgeBatchToLlm,
    SessionRequested,
    SessionState,
    ShutdownRequested,
    TimerFired,
    Trace,
)


def handle(coordinator, event):
    return asyncio.run(coordinator.handle(event))


def drive(coordinator, events):
    commands = []
    for event in events:
        commands.extend(handle(coordinator, event))
    return commands


def effects(commands):
    return [command for command in commands if not isinstance(command, Trace)]


def traces(commands):
    return [command for command in commands if isinstance(command, Trace)]


def window_timer():
    return TimerFired(connection_id=EVENT_WINDOW_TIMER_ID)


def close_window(coordinator):
    return handle(coordinator, window_timer())


def opened(commands):
    return next(c for c in commands if isinstance(c, OpenLiveConnection))


def batch(commands):
    return next(c for c in commands if isinstance(c, SendJudgeBatchToLlm))


def drops(commands, reason):
    return [
        t
        for t in traces(commands)
        if t.kind == "EventQueueDropped" and t.payload.get("reason") == reason
    ]


def visible_session(client_id="c1"):
    return [
        SessionRequested(client_id=client_id),
        LlmOpened(),
        LlmAudio(data=b"hi"),
    ]


def test_the_queue_is_capped_at_120_and_drops_the_oldest_each_time():
    coordinator = Coordinator(judge_prompt="JUGE")

    commands = []
    for index in range(EVENT_QUEUE_MAX + 1):
        commands.extend(
            handle(coordinator, EventReceived(source="m", content=f"fait-{index}"))
        )

    # Le plus ancien (fait-0) a été jeté à l'arrivée du 121e, et tracé.
    assert len(drops(commands, EventDropReason.CAPACITY)) == 1
    assert coordinator.state is SessionState.IDLE

    commands = close_window(coordinator)
    text = batch(commands).text
    lines = text.splitlines()[1:]
    assert len(lines) == EVENT_QUEUE_MAX
    assert lines[0] == "- [m] fait-1"
    assert lines[-1] == f"- [m] fait-{EVENT_QUEUE_MAX}"
    assert "fait-0" not in text


def test_an_exact_duplicate_in_the_current_window_is_not_judged():
    coordinator = Coordinator(judge_prompt="JUGE")

    handle(coordinator, EventReceived(source="m", content="porte"))
    commands = handle(coordinator, EventReceived(source="m", content="porte"))

    assert len(drops(commands, EventDropReason.DUPLICATE)) == 1

    text = batch(close_window(coordinator)).text
    assert text == "[ÉVÉNEMENTS]\n- [m] porte"


def test_a_different_source_or_content_is_not_a_duplicate():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [
            EventReceived(source="m", content="porte"),
            EventReceived(source="door", content="porte"),
            EventReceived(source="m", content="colis"),
        ],
    )

    text = batch(close_window(coordinator)).text
    assert text == (
        "[ÉVÉNEMENTS]\n- [m] porte\n- [door] porte\n- [m] colis"
    )


def test_duplicates_queued_hors_repos_are_collapsed_when_the_window_opens():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, visible_session())

    # Hors Repos : rien n'est jugé, les répétitions s'accumulent quand même.
    drive(
        coordinator,
        [
            EventReceived(source="m", content="un"),
            EventReceived(source="m", content="deux"),
            EventReceived(source="m", content="un"),
        ],
    )

    # Retour au Repos : la fenêtre s'ouvre et écrase le doublon (le plus ancien
    # est conservé), en traçant le rejet.
    commands = handle(coordinator, LlmClosed(connection_id="s1"))

    assert len(drops(commands, EventDropReason.DUPLICATE)) == 1

    text = batch(close_window(coordinator)).text
    assert text == "[ÉVÉNEMENTS]\n- [m] un\n- [m] deux"


def test_an_oversized_content_is_rejected_on_arrival_and_traced():
    coordinator = Coordinator(judge_prompt="JUGE")

    commands = handle(
        coordinator, EventReceived(source="hook", content="x" * (EVENT_MAX_CHARS + 1))
    )

    assert len(drops(commands, EventDropReason.TOO_LARGE)) == 1
    assert coordinator.state is SessionState.IDLE


def test_a_content_at_the_limit_is_kept_and_a_rejected_one_is_absent():
    coordinator = Coordinator(judge_prompt="JUGE")

    drive(
        coordinator,
        [
            EventReceived(source="hook", content="B" * (EVENT_MAX_CHARS + 1)),
            EventReceived(source="hook", content="A" * EVENT_MAX_CHARS),
        ],
    )

    text = batch(close_window(coordinator)).text
    assert text == "[ÉVÉNEMENTS]\n- [hook] " + "A" * EVENT_MAX_CHARS


def test_shutdown_drops_the_whole_pending_file_and_traces_each_event():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(
        coordinator,
        [
            EventReceived(source="m", content="un"),
            EventReceived(source="m", content="deux"),
        ],
    )

    commands = handle(coordinator, ShutdownRequested())

    assert len(drops(commands, EventDropReason.SHUTDOWN)) == 2
    assert OpenLiveConnection not in [type(c) for c in commands]
    assert SendJudgeBatchToLlm not in [type(c) for c in commands]
    assert coordinator.state is SessionState.IDLE


def test_no_queued_event_survives_the_shutdown():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, [EventReceived(source="m", content="un")])
    handle(coordinator, ShutdownRequested())

    handle(coordinator, EventReceived(source="m", content="apres"))

    text = batch(close_window(coordinator)).text
    assert text == "[ÉVÉNEMENTS]\n- [m] apres"


def test_a_legitimate_batch_under_all_limits_is_left_unchanged():
    coordinator = Coordinator(judge_prompt="JUGE")

    commands = drive(
        coordinator,
        [
            EventReceived(source="matrix", content="porte ouverte"),
            EventReceived(source="door", content="porte d'entrée"),
            EventReceived(source="matrix", content="colis"),
        ],
    )

    assert [t for t in traces(commands) if t.kind == "EventQueueDropped"] == []
    text = batch(close_window(coordinator)).text
    assert text == (
        "[ÉVÉNEMENTS]\n"
        "- [matrix] porte ouverte\n"
        "- [door] porte d'entrée\n"
        "- [matrix] colis"
    )


def test_preemption_requeue_still_honours_the_ceiling_and_traces_each_drop():
    coordinator = Coordinator(judge_prompt="JUGE")
    # File pleine (120) puis jugement : le lot sort de la File.
    for index in range(EVENT_QUEUE_MAX):
        handle(coordinator, EventReceived(source="m", content=f"lot-{index}"))
    handle(coordinator, window_timer())
    # Pendant le jugement, la File se remplit à nouveau jusqu'au plafond.
    for index in range(EVENT_QUEUE_MAX):
        handle(coordinator, EventReceived(source="m", content=f"recent-{index}"))

    commands = handle(coordinator, SessionRequested(client_id="c1"))

    # Les faits préemptés repassent devant : le plafond jette les plus anciens
    # (le lot préempté) et trace chaque rejet.
    assert len(drops(commands, EventDropReason.CAPACITY)) == EVENT_QUEUE_MAX

    # La Session utilisateur se termine ; le lot rejugé tient le plafond.
    drive(coordinator, [LlmOpened(), LlmAudio(data=b"hi")])
    handle(coordinator, LlmClosed(connection_id="s2"))
    text = batch(close_window(coordinator)).text
    lines = text.splitlines()[1:]
    assert len(lines) == EVENT_QUEUE_MAX
    assert lines[0] == "- [m] recent-0"
    assert lines[-1] == f"- [m] recent-{EVENT_QUEUE_MAX - 1}"
    assert "lot-0" not in text


