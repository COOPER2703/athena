"""Planificateur : boucle d'émission des Occurrences (ADR-0028, #195).

Composant **pur** du noyau : il ne lit ni l'horloge, ni le disque, ni
``asyncio``. Le magasin, l'instant courant, l'attente (``Wakeup``), l'émission
et les traces sont injectés par la racine de composition. Il ne fait que trois
choses, en boucle :

1. au démarrage, réclamer les échéances manquées — une ponctuelle est rattrapée
   une fois, une récurrente est avancée sans être rejouée et son saut est tracé ;
2. réclamer les échéances dues et émettre une **Occurrence** par Planification,
   puis tracer ``OccurrenceEmitted`` ;
3. attendre la prochaine échéance, ou un réveil (création d'une Planification
   plus proche) — jamais un sommeil réel dans les tests.

Le contenu d'une Planification reste une donnée : la boucle le transmet tel
quel, elle ne l'interprète ni ne l'exécute.
"""

from __future__ import annotations

from datetime import datetime
from typing import Awaitable, Callable, Mapping
from zoneinfo import ZoneInfo

from server.core.events import OccurrenceReceived
from server.core.ports import Schedule, ScheduleStore, Wakeup

#: Trace d'une Planification sortie du magasin, source ``planificateur``.
TRACE_SOURCE = "planificateur"
#: Sans ``Wakeup`` injecté ni Planification en attente, la boucle relit le
#: magasin à ce rythme. La production fournit toujours un ``Wakeup`` : le cas ne
#: concerne que les tests de la couture basse.
IDLE_RECHECK_SECONDS = 60.0

TraceCallback = Callable[[str, Mapping[str, object]], None]


class Planificateur:
    """Émet les Occurrences dues et dort jusqu'à la suivante."""

    def __init__(
        self,
        *,
        store: ScheduleStore,
        now: Callable[[], datetime],
        timezone: str | ZoneInfo,
        sleep: Callable[[float], Awaitable[None]],
        emit: Callable[[OccurrenceReceived], None],
        trace: TraceCallback,
        wake: Wakeup | None = None,
    ) -> None:
        self._store = store
        self._now = now
        self._timezone = timezone
        self._sleep = sleep
        self._emit = emit
        self._trace = trace
        self._wake = wake

    async def run(self) -> None:
        """Boucle de fond, annulée avec l'application (aucune tâche orpheline)."""
        await self._reconcile(startup=True)
        while True:
            await self._reconcile(startup=False)
            await self._wait()

    async def _reconcile(self, *, startup: bool) -> None:
        """Réclame et émet ; au démarrage, trace les récurrentes non rejouées."""
        now_epoch = self._now_epoch()
        missed: list[Schedule] = []
        if startup:
            missed = [
                schedule
                for schedule in self._store.list()
                if schedule.next_occurrence <= now_epoch
                and schedule.rule.kind != "once"
            ]
        claimed = self._store.claim_due(
            now_epoch, startup=startup, timezone=self._timezone
        )
        # La Trace de saut suit la réclamation : une transaction annulée n'a
        # jamais avancé la Planification, donc rien n'a été réellement sauté.
        for schedule in missed:
            self._trace(
                "OccurrenceSkipped",
                {
                    "schedule_id": schedule.id,
                    "kind": schedule.rule.kind,
                    "reason": "missed_slots",
                    "due": schedule.next_occurrence,
                    "now": now_epoch,
                },
            )
        for schedule in claimed:
            self._emit(
                OccurrenceReceived(
                    content=schedule.content, schedule_id=schedule.id
                )
            )
            # Après l'émission : une Trace ne précède jamais un effet qui
            # pourrait échouer (aucune réussite mensongère).
            self._trace(
                "OccurrenceEmitted",
                {
                    "schedule_id": schedule.id,
                    "kind": schedule.rule.kind,
                    "due": schedule.next_occurrence,
                },
            )

    async def _wait(self) -> None:
        """Dort jusqu'à la prochaine échéance, ou jusqu'à un réveil."""
        delay = self._delay_until_next()
        if delay is not None and delay <= 0:
            # Échéance atteinte : la boucle réclame sans dormir.
            return
        if self._wake is not None:
            await self._wake.wait(delay)
        elif delay is not None:
            await self._sleep(delay)
        else:
            await self._sleep(IDLE_RECHECK_SECONDS)

    def _delay_until_next(self) -> float | None:
        schedules = self._store.list()
        if not schedules:
            return None
        return max(schedules[0].next_occurrence - self._now_epoch(), 0.0)

    def _now_epoch(self) -> int:
        return int(self._now().timestamp())
