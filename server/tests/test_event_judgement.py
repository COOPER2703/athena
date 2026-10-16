"""Couture pure du jugement des Événements (ADR-0007 / ADR-0010, ticket #38).

On n'observe que le comportement externe de ``Coordinator.handle`` : une liste
d'Événements en entrée, une liste de Commandes en sortie. Aucune I/O.
"""

from __future__ import annotations

import asyncio

from server.core import (
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


def opened(commands):
    return next(c for c in commands if isinstance(c, OpenLiveConnection))


def batch(commands):
    return next(c for c in commands if isinstance(c, SendJudgeBatchToLlm))


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


def visible_session(client_id="c1"):
    return [
        SessionRequested(client_id=client_id),
        LlmOpened(),
        LlmAudio(data=b"hi"),
    ]


def test_event_at_repos_opens_one_judgment_connection_with_the_dedicated_instruction():
    coordinator = Coordinator(judge_prompt="JUGE")

    commands = handle(
        coordinator, EventReceived(source="matrix", content="porte ouverte")
    )

    assert effects(commands) == [
        OpenLiveConnection(connection_id="s1", instruction="JUGE", tools=()),
        StartIdleTimer(connection_id="s1", timeout=JUDGE_TIMEOUT),
        SendJudgeBatchToLlm(
            connection_id="s1", text="[ÉVÉNEMENTS]\n- [matrix] porte ouverte"
        ),
    ]
    assert coordinator.state is SessionState.LIVE_CONNECTING


def test_judgment_uses_the_dedicated_prompt_without_any_memory_recall():
    memory = FakeMemory(context="PROFIL")
    coordinator = Coordinator(memory=memory, user_id="alice", judge_prompt="JUGE")

    commands = handle(coordinator, EventReceived(source="matrix", content="x"))

    assert opened(commands).instruction == "JUGE"
    assert memory.users == []
    assert SendMemoryToLlm not in [type(c) for c in commands]


def test_event_during_a_visible_session_is_queued_and_never_injected():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, visible_session())

    commands = handle(coordinator, EventReceived(source="matrix", content="porte"))

    assert effects(commands) == []
    assert coordinator.state is SessionState.SESSION_VISIBLE


def test_events_received_during_a_session_are_judged_at_repos_in_arrival_order():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, visible_session())
    handle(coordinator, EventReceived(source="a", content="premier"))
    handle(coordinator, EventReceived(source="b", content="second"))

    commands = handle(coordinator, LlmClosed(connection_id="s1"))

    assert opened(commands).connection_id == "s2"
    assert batch(commands).text == "[ÉVÉNEMENTS]\n- [a] premier\n- [b] second"
    assert AnnounceSessionEnded in [type(c) for c in commands]


def test_speaking_judgment_announces_an_auto_session_to_the_registered_client():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, [ClientRegistered(client_id="c9")])
    drive(
        coordinator,
        [EventReceived(source="matrix", content="porte"), LlmOpened()],
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
        [EventReceived(source="m", content="un"), LlmOpened()],
    )

    commands = handle(coordinator, EventReceived(source="m", content="deux"))

    assert effects(commands) == []
    assert coordinator.state is SessionState.LIVE_ACTIVE

    commands = handle(coordinator, TimerFired(connection_id="s1"))
    assert batch(commands).text == "[ÉVÉNEMENTS]\n- [m] deux"


def test_adapter_audio_is_processed_immediately_while_judging():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, [EventReceived(source="m", content="x"), LlmOpened()])

    commands = handle(coordinator, ClientAudio(data=b"mic"))

    assert SendAudioToLlm(connection_id="s1", data=b"mic") in effects(commands)


def test_judgment_connection_counts_so_a_second_event_does_not_open_a_new_one():
    coordinator = Coordinator(judge_prompt="JUGE")
    drive(coordinator, [EventReceived(source="m", content="un"), LlmOpened()])

    commands = handle(coordinator, EventReceived(source="m", content="deux"))

    assert OpenLiveConnection not in [type(c) for c in commands]


def test_judging_event_is_traced():
    coordinator = Coordinator(judge_prompt="JUGE")

    commands = handle(coordinator, EventReceived(source="matrix", content="x"))

    assert any(isinstance(c, Trace) for c in commands)
    assert traces(commands)


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

