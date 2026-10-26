"""Consommateur borné du flux unique de traces pour la Console web.

La Console n'ouvre pas un second canal : elle consomme le flux déjà produit par
le fan-out (ADR-0004). Ce module garde les dernières entrées en mémoire pour les
rejouer à la connexion, et distribue les nouvelles entrées aux abonnés.

Toutes les entrées sont conservées, quel que soit leur niveau : le filtrage est
une affaire de présentation, comme pour ``ConsoleTraceSink`` (ADR-0004).

Le tampon et ses abonnés vivent dans la boucle asyncio du serveur. ``emit`` et
``subscribe`` sont donc synchrones : la lecture du backlog et l'enregistrement
d'un abonné forment une seule opération atomique, sans point d'``await`` entre
les deux. C'est ce qui garantit l'absence de trou et de doublon à la connexion.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Deque

from server.core.ports import TraceEntry

DEFAULT_TRACE_BUFFER = 500
# Kind réservé du marqueur de troncature : une trame ``trace`` ordinaire, pour
# qu'un client qui ignore ce kind puisse au moins l'afficher comme une Trace.
TRUNCATION_KIND = "TraceTruncated"
TRUNCATION_SOURCE = "console"
TRUNCATION_LEVEL = "warning"


def trace_to_json(entry: TraceEntry) -> dict[str, object]:
    """Forme sérialisable d'une Trace, telle que le contrat JSON la définit."""
    return {
        "timestamp": entry.timestamp,
        "source": entry.source,
        "kind": entry.kind,
        "level": entry.level,
        "payload": dict(entry.payload),
    }


def truncation_entry(dropped: int) -> TraceEntry:
    """Marqueur unique signalant des entrées perdues par un client trop lent."""
    return TraceEntry(
        timestamp=time.time(),
        source=TRUNCATION_SOURCE,
        kind=TRUNCATION_KIND,
        payload={"dropped": dropped},
        level=TRUNCATION_LEVEL,
    )


class TraceSubscription(AsyncIterator[TraceEntry]):
    """File bornée d'un client : perd les plus anciennes entrées, pas les neuves.

    Un seul marqueur de troncature est émis par connexion, au premier
    dépassement : il précède les entrées conservées, et son payload indique
    combien d'entrées ont été jetées à ce moment-là. Les pertes suivantes ne
    répètent pas le marqueur (le client sait déjà que son flux est incomplet).
    """

    def __init__(
        self,
        backlog: tuple[TraceEntry, ...],
        capacity: int,
    ) -> None:
        if capacity < 1:
            raise ValueError("Trace buffer capacity must be >= 1")
        self._backlog = backlog
        self._capacity = capacity
        self._queue: Deque[TraceEntry] = deque()
        self._wakeup = asyncio.Event()
        self._closed = False
        self._dropped = 0
        self._marker_pending = False
        self._marker_sent = False

    @property
    def backlog(self) -> tuple[TraceEntry, ...]:
        """Entrées déjà présentes au moment exact de l'abonnement."""
        return self._backlog

    def __aiter__(self) -> "TraceSubscription":
        return self

    async def __anext__(self) -> TraceEntry:
        while not self._queue and not self._closed:
            self._wakeup.clear()
            # Re-vérifier après ``clear`` : ``offer`` a pu remplir la file entre
            # le test et l'effacement du drapeau.
            if self._queue or self._closed:
                break
            await self._wakeup.wait()
        if self._queue:
            if self._marker_pending:
                self._marker_pending = False
                self._marker_sent = True
                return truncation_entry(self._dropped)
            return self._queue.popleft()
        raise StopAsyncIteration

    def offer(self, entry: TraceEntry) -> None:
        """Ajoute une entrée ; jette la plus ancienne si la file est pleine."""
        if self._closed:
            return
        if len(self._queue) >= self._capacity:
            self._queue.popleft()
            self._dropped += 1
            if not self._marker_sent:
                self._marker_pending = True
        self._queue.append(entry)
        self._wakeup.set()

    def close(self) -> None:
        """Réveille le lecteur et termine l'itération."""
        self._closed = True
        self._wakeup.set()


class TraceBuffer:
    """Dernières Traces du flux unique, et abonnés du direct.

    ``capacity`` borne la mémoire du tampon (les plus anciennes entrées sont
    oubliées). Chaque abonné a sa propre file de la même taille : un client lent
    ne ralentit ni le noyau ni les autres clients.
    """

    def __init__(self, capacity: int = DEFAULT_TRACE_BUFFER) -> None:
        if capacity < 1:
            raise ValueError("Trace buffer capacity must be >= 1")
        self._capacity = capacity
        self._entries: Deque[TraceEntry] = deque(maxlen=capacity)
        self._subscribers: list[TraceSubscription] = []

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def emit(self, entry: TraceEntry) -> None:
        """Point d'entrée du fan-out : conserve puis diffuse l'entrée."""
        self._entries.append(entry)
        for subscription in tuple(self._subscribers):
            subscription.offer(entry)

    def subscribe(self) -> TraceSubscription:
        """Abonne un client : backlog figé + enregistrement, sans ``await``.

        L'appel est synchrone et s'exécute dans la boucle unique : aucune entrée
        ne peut s'intercaler entre la copie du backlog et l'enregistrement de
        l'abonné. Une entrée émise juste après est donc diffusée au client, une
        entrée émise juste avant est déjà dans son backlog.
        """
        subscription = TraceSubscription(tuple(self._entries), self._capacity)
        self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: TraceSubscription) -> None:
        """Retire un abonné à la déconnexion (aucune file ne fuit)."""
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)
        subscription.close()

    def close(self) -> None:
        """Termine tous les abonnés : l'arrêt ferme proprement les sockets."""
        for subscription in tuple(self._subscribers):
            subscription.close()
        self._subscribers.clear()
