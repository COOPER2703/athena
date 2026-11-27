"""Présence des Clients devant la File d'attente (ADR-0028, ticket #193).

Couture pure ``Coordinator.handle`` : la présence est une couture injectée
(``connected_clients``), le test la fait varier entre deux Événements. On
observe qu'aucune fenêtre de jugement, aucune Connexion Live et aucune Annonce
ne s'ouvrent sans Client **réellement connecté**, que la File garde l'ordre
d'arrivée, et qu'une Annonce est adressée à un Client connecté — jamais à un
``client_id`` périmé après une déconnexion.
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
)
from testkit import FakeJudge
from server.core import (
    EVENT_QUEUE_MAX,
    EVENT_WINDOW_TIMER_ID,
    EVENT_WINDOW_TIMEOUT,
    AnnounceSessionStarted,
    ClientDisconnected,
    ClientRegistered,
    Coordinator,
    EventDropReason,
    EventReceived,
    LlmAudio,
    LlmOpened,
    SessionState,
    StartIdleTimer,
)


def deferred(commands) -> list:
    return [
        trace
        for trace in traces(commands)
        if trace.kind == "EventDeferred"
        and trace.payload.get("reason") == "no_client"
    ]


def at_rest(*client_ids: str, announce: bool = True) -> tuple[Coordinator, Presence, FakeJudge]:
    """Noyau au Repos avec une présence simulée et un juge de test."""
    judge = FakeJudge(message="Porte")
    presence = Presence(*client_ids)
    coordinator = Coordinator(
        announce_prompt="JUGE",
        judge=judge,
        connected_clients=presence,
    )
    return coordinator, presence, judge


def test_an_event_at_rest_without_a_client_waits_and_is_traced():
    coordinator, _presence, judge = at_rest()

    commands = handle(coordinator, EventReceived(source="matrix", content="porte"))

    # Ni fenêtre armée, ni Connexion Live : l'Événement patiente au Repos.
    assert effects(commands) == []
    assert deferred(commands)
    assert coordinator.state is SessionState.IDLE
    assert judge.calls == []


def test_the_first_registered_client_opens_the_window_and_the_deadline_judges():
    coordinator, presence, judge = at_rest()

    handle(coordinator, EventReceived(source="matrix", content="porte"))
    presence.connect("c1")
    commands = handle(coordinator, ClientRegistered(client_id="c1"))

    # L'enregistrement seul ouvre la fenêtre, sans nouvel Événement.
    assert effects(commands) == [
        StartIdleTimer(
            connection_id=EVENT_WINDOW_TIMER_ID, timeout=EVENT_WINDOW_TIMEOUT
        )
    ]

    commands = close_window(coordinator)

    assert judge.calls[-1] == ["[matrix] porte"]
    assert announcement(commands).text == "Porte"
    assert opened(commands).connection_id == coordinator.connection_id


def test_the_deadline_defers_when_the_last_client_left_in_the_meantime():
    coordinator, presence, judge = at_rest("c1")
    handle(coordinator, ClientRegistered(client_id="c1"))
    handle(coordinator, EventReceived(source="matrix", content="porte"))

    disconnect = handle(coordinator, ClientDisconnected(client_id="c1"))
    presence.disconnect("c1")
    # La déconnexion ne touche pas la Session (il n'y en a pas) et n'annule
    # pas la fenêtre : c'est l'échéance qui re-vérifie la présence.
    assert coordinator.state is SessionState.IDLE
    assert effects(disconnect) == []

    commands = close_window(coordinator)

    assert effects(commands) == []
    assert deferred(commands)
    assert judge.calls == []

    # Le prochain Client enregistré rouvre la fenêtre et le lot est jugé,
    # toujours dans l'ordre d'arrivée.
    presence.connect("c2")
    commands = handle(coordinator, ClientRegistered(client_id="c2"))
    assert effects(commands) == [
        StartIdleTimer(
            connection_id=EVENT_WINDOW_TIMER_ID, timeout=EVENT_WINDOW_TIMEOUT
        )
    ]
    commands = close_window(coordinator)
    assert judge.calls[-1] == ["[matrix] porte"]
    assert announcement(commands).text == "Porte"


def test_the_announcement_targets_a_connected_client_not_a_stale_one():
    coordinator, presence, _judge = at_rest("a")
    handle(coordinator, ClientRegistered(client_id="a"))
    handle(coordinator, EventReceived(source="matrix", content="porte"))

    # A disparaît avant l'échéance, B se connecte : le Client enregistré est
    # désormais périmé, l'Annonce doit viser B.
    handle(coordinator, ClientDisconnected(client_id="a"))
    presence.disconnect("a")
    presence.connect("b")
    commands = handle(coordinator, ClientRegistered(client_id="b"))
    # La fenêtre est déjà ouverte : pas de nouvel armement.
    assert effects(commands) == []

    commands = close_window(coordinator)
    opened_connection = opened(commands)
    assert announcement(commands).connection_id == opened_connection.connection_id
    # Aucun rejet, aucune perte silencieuse pour un client inconnu.
    assert not [
        trace
        for trace in traces(commands)
        if trace.kind == "EventQueueDropped"
    ]

    handle(coordinator, LlmOpened())
    commands = handle(coordinator, LlmAudio(data=b"bonjour"))

    assert AnnounceSessionStarted(client_id="b", auto=True) in commands


def test_a_non_active_client_disconnecting_does_not_touch_the_session():
    coordinator, presence, _judge = at_rest("a", "b")
    drive(coordinator, visible_session("a"))

    commands = handle(coordinator, ClientDisconnected(client_id="b"))

    assert effects(commands) == []
    assert coordinator.state is SessionState.SESSION_VISIBLE
    assert coordinator.owner_client_id == "a"
    # La présence rétrécit, sans autre effet.
    presence.disconnect("b")
    assert presence() == ["a"]


def test_the_queue_stays_bounded_and_traced_while_no_client_comes():
    coordinator, presence, judge = at_rest()

    commands = []
    for index in range(EVENT_QUEUE_MAX + 1):
        commands.extend(
            handle(coordinator, EventReceived(source="m", content=f"fait-{index}"))
        )

    drops = [
        trace
        for trace in traces(commands)
        if trace.kind == "EventQueueDropped"
        and trace.payload.get("reason") == EventDropReason.CAPACITY.value
    ]
    assert len(drops) == 1

    presence.connect("c1")
    handle(coordinator, ClientRegistered(client_id="c1"))
    close_window(coordinator)

    facts = judge.calls[-1]
    assert len(facts) == EVENT_QUEUE_MAX
    # Ordre d'arrivée : le plus ancien restant d'abord.
    assert facts[0] == "[m] fait-1"
    assert facts[-1] == f"[m] fait-{EVENT_QUEUE_MAX}"


def test_without_a_presence_seam_the_kernel_keeps_its_legacy_behaviour():
    """``None`` = présence inconnue, réservé aux tests et à la compatibilité.

    ``build_app`` injecte **toujours** une vraie lecture ; le défaut ne doit
    donc jamais devenir la règle de production.
    """
    coordinator = Coordinator(announce_prompt="JUGE")

    commands = handle(coordinator, EventReceived(source="matrix", content="porte"))

    assert effects(commands) == [
        StartIdleTimer(
            connection_id=EVENT_WINDOW_TIMER_ID, timeout=EVENT_WINDOW_TIMEOUT
        )
    ]
    assert not deferred(commands)


def test_an_announcement_deferred_for_lack_of_client_is_traced_with_the_batch_size():
    coordinator, presence, _judge = at_rest("c1")
    handle(coordinator, ClientRegistered(client_id="c1"))
    handle(coordinator, EventReceived(source="m", content="un"))
    handle(coordinator, EventReceived(source="m", content="deux"))
    # Le client s'en va entre l'armement et l'échéance.
    presence.disconnect("c1")

    commands = close_window(coordinator)

    [trace] = deferred(commands)
    assert trace.payload.get("events") == 2
    assert effects(commands) == []
