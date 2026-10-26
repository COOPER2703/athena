"""Diffusion de l'état vivant de la Console web.

L'instantané est déjà servi à la demande par ``GET /api/status``. Le WebSocket
``/api/stream`` pousse en plus une trame ``status`` quand — et seulement quand —
l'instantané a changé. La comparaison est structurelle : elle repose sur une
sérialisation stable (clés triées), pas sur l'identité des objets rendus par la
racine de composition.

Ce module ne connaît ni aiohttp ni le noyau : il possède seulement une tâche de
fond et ses abonnés, tous dans la boucle asyncio unique du serveur.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping

# Tick par défaut : environ une seconde. Les tests injectent un intervalle plus
# court pour rester rapides.
DEFAULT_STATUS_INTERVAL = 1.0


def stable_snapshot(snapshot: Mapping[str, object]) -> str:
    """Empreinte déterministe d'un instantané JSON.

    Les clés sont triées : deux dicts de même contenu mais d'ordre d'insertion
    différent produisent la même empreinte et ne déclenchent donc pas de trame.
    """
    return json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


class StatusBroadcaster:
    """Tâche de fond qui détecte les changements d'instantané et les diffuse.

    Chaque abonné ne garde que le dernier instantané en attente : un état est
    complet, donc une valeur plus récente remplace une plus ancienne sans perte
    d'information. Un client lent ne ralentit ni le tick ni les autres clients.
    """

    def __init__(
        self,
        snapshot: Callable[[], Mapping[str, object]],
        *,
        interval: float = DEFAULT_STATUS_INTERVAL,
    ) -> None:
        if interval <= 0:
            raise ValueError("Status interval must be > 0")
        self._snapshot = snapshot
        self._interval = interval
        self._subscribers: list[StatusSubscription] = []
        # Empreinte de départ, prise de façon synchrone à la construction : un
        # changement survenu après `start` sera donc vu au premier tick, même
        # si un client s'abonne entre-temps.
        self._last = stable_snapshot(snapshot())

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> "StatusSubscription":
        subscription = StatusSubscription()
        self._subscribers.append(subscription)
        return subscription

    def unsubscribe(self, subscription: "StatusSubscription") -> None:
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)
        subscription.close()

    def close(self) -> None:
        for subscription in tuple(self._subscribers):
            subscription.close()
        self._subscribers.clear()

    async def run(self) -> None:
        """Tick de fond : compare, puis diffuse seulement en cas de changement."""
        while True:
            await asyncio.sleep(self._interval)
            current = self._snapshot()
            fingerprint = stable_snapshot(current)
            if fingerprint == self._last:
                continue
            self._last = fingerprint
            # Détache l'état de l'objet mutable de la racine : la trame envoyée
            # reflète le tick où le changement a été vu, pas un état plus tardif.
            payload = json.loads(fingerprint)
            for subscription in tuple(self._subscribers):
                subscription.offer(payload)


class StatusSubscription(AsyncIterator[Mapping[str, object]]):
    """Abonné d'état : une seule valeur en attente, la plus récente."""

    def __init__(self) -> None:
        self._pending: Mapping[str, object] | None = None
        self._wakeup = asyncio.Event()
        self._closed = False

    def __aiter__(self) -> "StatusSubscription":
        return self

    async def __anext__(self) -> Mapping[str, object]:
        while self._pending is None and not self._closed:
            self._wakeup.clear()
            if self._pending is not None or self._closed:
                break
            await self._wakeup.wait()
        if self._pending is None:
            raise StopAsyncIteration
        pending = self._pending
        self._pending = None
        return pending

    def offer(self, snapshot: Mapping[str, object]) -> None:
        if self._closed:
            return
        self._pending = snapshot
        self._wakeup.set()

    def close(self) -> None:
        self._closed = True
        self._wakeup.set()
