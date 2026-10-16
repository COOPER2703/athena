"""Couture pure de la fenêtre de regroupement des Événements (ADR-0007, #39).

On n'observe que le comportement externe de ``Coordinator.handle`` : une liste
d'Événements en entrée, une liste de Commandes en sortie. Aucune I/O, aucun
temps réel : la fenêtre est close en injectant ``TimerFired`` sur la **clé
réservée** que le noyau a armée, exactement ce que fera l'horloge à l'échéance.
"""

from __future__ import annotations

import asyncio

from server.core import (
    EVENT_WINDOW_TIMER_ID,
    EVENT_WINDOW_TIMEOUT,
    IDLE_TIMEOUT,
    JUDGE_TIMEOUT,
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    ClientAudio,
    ClientRegistered,
    CloseLiveConnection,
    Coordinator,
    EventReceived,
    LlmAudio,
    LlmClosed,
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
    """L'échéance de la fenêtre, telle que l'horloge la livre."""
    return TimerFired(connection_id=EVENT_WINDOW_TIMER_ID)


def close_window(coordinator):
    return handle(coordinator, window_timer())


def opened(commands):
    return next(c for c in commands if isinstance(c, OpenLiveConnection))


def batch(commands):
    return next(c for c in commands if isinstance(c, SendJudgeBatchToLlm))


class FakeMemory:
    def __init__(self, context: str = "") -> None:
        self.context = context
        self.users: list[str] = []

    async def session_context(self, user_id: str) -> str:
        self.users.append(user_id)
        return self.context

    async def pinned_facts(self, user_id: str) -> list[str]:
        return []

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        pass


def visible_session(client_id="c1"):
    return [
        SessionRequested(client_id=client_id),
        LlmOpened(),
        LlmAudio(data=b"hi"),
    ]


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
