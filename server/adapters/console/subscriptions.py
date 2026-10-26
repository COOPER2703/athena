"""Socle commun des abonnés du flux d'observation de la Console.

Le diffuseur d'état et le tampon de traces ont le même besoin : réveiller un
lecteur asynchrone sans attente active, le fermer proprement, et lui offrir des
valeurs selon une politique de perte explicite. Ce module porte ce motif une
seule fois ; chaque surface garde seulement sa politique (dernière valeur pour
l'état, file bornée avec marqueur pour les traces).

Tout vit dans la boucle asyncio unique du serveur : ``offer``, ``close`` et
l'enregistrement des abonnés sont synchrones, sans point d'``await``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator
from typing import Deque, Generic, TypeVar

T = TypeVar("T")


class Subscription(AsyncIterator[T], ABC):
    """Abonné d'un diffuseur : réveil, fermeture et itération sans attente active."""

    def __init__(self) -> None:
        self._wakeup = asyncio.Event()
        self._closed = False

    def __aiter__(self) -> "Subscription[T]":
        return self

    async def __anext__(self) -> T:
        while not self._has_pending() and not self._closed:
            self._wakeup.clear()
            # Re-vérifier après ``clear`` : ``offer`` a pu remplir la file entre
            # le test et l'effacement du drapeau.
            if self._has_pending() or self._closed:
                break
            await self._wakeup.wait()
        if not self._has_pending():
            raise StopAsyncIteration
        return self._take()

    def close(self) -> None:
        """Réveille le lecteur et termine l'itération."""
        self._closed = True
        self._wakeup.set()

    def _wake(self) -> None:
        self._wakeup.set()

    @abstractmethod
    def _has_pending(self) -> bool:
        """Vrai si une valeur est disponible sans attendre."""

    @abstractmethod
    def _take(self) -> T:
        """Retire et rend la prochaine valeur ; il y en a une."""

    @abstractmethod
    def offer(self, item: T) -> None:
        """Dépose une valeur pour le lecteur, selon la politique du diffuseur."""


class LatestValueSubscription(Subscription[T]):
    """Une seule valeur en attente : la plus récente remplace la précédente.

    Adapté à un état complet : une valeur plus récente porte toute
    l'information, donc aucune perte n'a besoin d'être signalée.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending: T | None = None

    def _has_pending(self) -> bool:
        return self._pending is not None

    def _take(self) -> T:
        pending = self._pending
        assert pending is not None
        self._pending = None
        return pending

    def offer(self, item: T) -> None:
        if self._closed:
            return
        self._pending = item
        self._wake()


class BoundedSubscription(Subscription[T]):
    """File bornée : jette la plus ancienne valeur quand elle est pleine.

    La sous-classe décide comment signaler les pertes via ``_on_drop`` ; le
    socle se contente de les compter.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("Subscription capacity must be >= 1")
        super().__init__()
        self._capacity = capacity
        self._queue: Deque[T] = deque()
        self._dropped = 0

    @property
    def dropped(self) -> int:
        """Nombre cumulé de valeurs perdues par cet abonné."""
        return self._dropped

    def _has_pending(self) -> bool:
        return bool(self._queue)

    def _take(self) -> T:
        return self._queue.popleft()

    def offer(self, item: T) -> None:
        if self._closed:
            return
        if len(self._queue) >= self._capacity:
            self._queue.popleft()
            self._dropped += 1
            self._on_drop()
        self._queue.append(item)
        self._wake()

    def _on_drop(self) -> None:
        """Crochet appelé à chaque valeur perdue ; sans effet par défaut."""


class SubscriptionHub(Generic[T]):
    """Registre des abonnés d'un diffuseur : ajout, retrait, offre, fermeture."""

    def __init__(self) -> None:
        self._subscriptions: list[Subscription[T]] = []

    @property
    def subscriber_count(self) -> int:
        return len(self._subscriptions)

    def add(self, subscription: Subscription[T]) -> None:
        self._subscriptions.append(subscription)

    def remove(self, subscription: Subscription[T]) -> None:
        """Retire un abonné à la déconnexion (aucune file ne fuit)."""
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)
        subscription.close()

    def offer(self, item: T) -> None:
        """Offre la valeur à tous les abonnés, sans ralentir les autres."""
        for subscription in tuple(self._subscriptions):
            subscription.offer(item)

    def close(self) -> None:
        """Termine tous les abonnés : l'arrêt ferme proprement les sockets."""
        for subscription in tuple(self._subscriptions):
            subscription.close()
        self._subscriptions.clear()
