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
from collections.abc import Callable, Mapping

from server.adapters.console.subscriptions import (
    LatestValueSubscription,
    SubscriptionHub,
)

# Tick par défaut : environ une seconde. Les tests injectent un intervalle plus
# court pour rester rapides.
DEFAULT_STATUS_INTERVAL = 1.0


def snapshot_fingerprint(snapshot: Mapping[str, object]) -> str:
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
        self._subscribers: SubscriptionHub[Mapping[str, object]] = SubscriptionHub()
        # Empreinte de départ, prise de façon synchrone à la construction : un
        # changement survenu après `start` sera donc vu au premier tick, même
        # si un client s'abonne entre-temps.
        self._last = snapshot_fingerprint(snapshot())

    @property
    def subscriber_count(self) -> int:
        return self._subscribers.subscriber_count

    def subscribe(self) -> "StatusSubscription":
        subscription = StatusSubscription()
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: "StatusSubscription") -> None:
        self._subscribers.remove(subscription)

    def close(self) -> None:
        self._subscribers.close()

    async def run(self) -> None:
        """Tick de fond : compare, puis diffuse seulement en cas de changement."""
        while True:
            await asyncio.sleep(self._interval)
            current = self._snapshot()
            fingerprint = snapshot_fingerprint(current)
            if fingerprint == self._last:
                continue
            self._last = fingerprint
            # Détache l'état de l'objet mutable de la racine : la trame envoyée
            # reflète le tick où le changement a été vu, pas un état plus tardif.
            payload = json.loads(fingerprint)
            self._subscribers.offer(payload)


class StatusSubscription(LatestValueSubscription[Mapping[str, object]]):
    """Abonné d'état : une seule valeur en attente, la plus récente."""
