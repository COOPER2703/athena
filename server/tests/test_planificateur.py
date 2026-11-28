"""Boucle du Planificateur (ADR-0028, #195), sans horloge ni sommeil réels.

La couture est ``Planificateur.run`` : magasin, ``now``, fusion du sommeil par
``Wakeup``, émission et traces sont injectés. On observe ce que la boucle
**émet** et **trace** — jamais sa structure interne.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from testkit import wait_until
from server.core.events import OccurrenceReceived
from server.core.planificateur import Planificateur
from server.core.ports import Schedule, ScheduleRule

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _schedule(
    *,
    schedule_id: str,
    next_occurrence: int,
    rule: ScheduleRule,
    content: str = "appeler le plombier",
) -> Schedule:
    return Schedule(
        id=schedule_id,
        content=content,
        rule=rule,
        next_occurrence=next_occurrence,
        created_at=1_700_000_000,
        updated_at=1_700_000_000,
    )


class ScriptedStore:
    """Magasin de couture : réponses scriptées par appel, ordre conservé."""

    def __init__(self, schedules: list[Schedule] | None = None) -> None:
        self.schedules = list(schedules or [])
        self.claims: list[tuple[int, bool, str | None]] = []
        self.claim_results: list[tuple[Schedule, ...]] = []

    def list(self) -> tuple[Schedule, ...]:
        return tuple(self.schedules)

    def claim_due(self, now_epoch, *, startup=False, timezone=None):
        self.claims.append((now_epoch, startup, timezone))
        if startup:
            # Le vrai magasin avance les récurrentes dues : la couture les
            # retire pour que la boucle ne les revoie pas comme encore dues.
            self.schedules = [
                schedule
                for schedule in self.schedules
                if not (
                    schedule.next_occurrence <= now_epoch
                    and schedule.rule.kind != "once"
                )
            ]
            return ()
        result = self.claim_results.pop(0) if self.claim_results else ()
        self.schedules = [
            schedule for schedule in self.schedules if schedule not in result
        ]
        return result


class ManualWakeup:
    """Réveil piloté : ``wait`` ne rend la main qu'à ``release`` ou ``notify``."""

    def __init__(self) -> None:
        self.waits: list[float | None] = []
        self._release = asyncio.Event()

    def notify(self) -> None:
        self._release.set()

    async def wait(self, timeout: float | None = None) -> None:
        self.waits.append(timeout)
        await self._release.wait()
        self._release.clear()

    def release(self) -> None:
        self._release.set()


async def _run_until_first_wait(planificateur: Planificateur, wakeup: ManualWakeup):
    task = asyncio.create_task(planificateur.run())
    try:
        await wait_until(lambda: bool(wakeup.waits))
        return task
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


def test_a_due_schedule_is_emitted_and_traced_at_the_injected_instant():
    async def scenario() -> None:
        due = _schedule(
            schedule_id="sch_1",
            next_occurrence=int(NOW.timestamp()) - 5,
            rule=ScheduleRule(kind="once", at=int(NOW.timestamp()) - 5),
            content="sortir les poubelles",
        )
        store = ScriptedStore()
        store.claim_results = [(due,)]
        emitted: list[OccurrenceReceived] = []
        traces: list[tuple[str, dict]] = []
        wakeup = ManualWakeup()
        planificateur = Planificateur(
            store=store,
            now=lambda: NOW,
            timezone=PARIS,
            sleep=asyncio.sleep,
            emit=emitted.append,
            trace=lambda kind, payload: traces.append((kind, dict(payload))),
            wake=wakeup,
        )

        task = await _run_until_first_wait(planificateur, wakeup)
        try:
            assert emitted == [
                OccurrenceReceived(
                    content="sortir les poubelles", schedule_id="sch_1"
                )
            ]
            emitted_trace = next(
                payload for kind, payload in traces if kind == "OccurrenceEmitted"
            )
            assert emitted_trace == {
                "schedule_id": "sch_1",
                "kind": "once",
                "due": int(NOW.timestamp()) - 5,
            }
            # Le fuseau du Réglage part avec la réclamation.
            assert store.claims[0] == (int(NOW.timestamp()), True, PARIS)
            assert store.claims[1] == (int(NOW.timestamp()), False, PARIS)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_startup_recurring_miss_is_skipped_without_emission():
    async def scenario() -> None:
        missed = _schedule(
            schedule_id="sch_tick",
            next_occurrence=int(NOW.timestamp()) - 600,
            rule=ScheduleRule(kind="interval", every_seconds=60),
        )
        store = ScriptedStore([missed])
        emitted: list[OccurrenceReceived] = []
        traces: list[tuple[str, dict]] = []
        wakeup = ManualWakeup()
        planificateur = Planificateur(
            store=store,
            now=lambda: NOW,
            timezone=PARIS,
            sleep=asyncio.sleep,
            emit=emitted.append,
            trace=lambda kind, payload: traces.append((kind, dict(payload))),
            wake=wakeup,
        )

        task = await _run_until_first_wait(planificateur, wakeup)
        try:
            assert emitted == []
            skipped = [
                payload for kind, payload in traces if kind == "OccurrenceSkipped"
            ]
            assert skipped == [
                {
                    "schedule_id": "sch_tick",
                    "kind": "interval",
                    "due": int(NOW.timestamp()) - 600,
                }
            ]
            # Le rattrapage de démarrage est réclamé avec ``startup=True``.
            assert store.claims[0][1] is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_the_pump_waits_exactly_until_the_earliest_next_occurrence():
    async def scenario() -> None:
        future = _schedule(
            schedule_id="sch_future",
            next_occurrence=int(NOW.timestamp()) + 600,
            rule=ScheduleRule(kind="interval", every_seconds=600),
        )
        store = ScriptedStore([future])
        wakeup = ManualWakeup()
        planificateur = Planificateur(
            store=store,
            now=lambda: NOW,
            timezone=PARIS,
            sleep=asyncio.sleep,
            emit=lambda occurrence: None,
            trace=lambda kind, payload: None,
            wake=wakeup,
        )

        task = await _run_until_first_wait(planificateur, wakeup)
        try:
            assert wakeup.waits[-1] == 600.0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_creation_wakes_the_pump_and_the_closer_schedule_fires():
    async def scenario() -> None:
        store = ScriptedStore()
        wakeup = ManualWakeup()
        emitted: list[OccurrenceReceived] = []
        planificateur = Planificateur(
            store=store,
            now=lambda: NOW,
            timezone=PARIS,
            sleep=asyncio.sleep,
            emit=emitted.append,
            trace=lambda kind, payload: None,
            wake=wakeup,
        )

        task = await _run_until_first_wait(planificateur, wakeup)
        try:
            # Aucune Planification : le pump attend indéfiniment un réveil.
            assert wakeup.waits[-1] is None
            # La couture ``on_change`` d'une création réveille la boucle.
            closer = _schedule(
                schedule_id="sch_close",
                next_occurrence=int(NOW.timestamp()),
                rule=ScheduleRule(kind="once", at=int(NOW.timestamp())),
            )
            store.schedules = [closer]
            store.claim_results = [(closer,)]
            wakeup.notify()
            await wait_until(lambda: bool(emitted))
            assert emitted[0].schedule_id == "sch_close"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_cancellation_ends_the_loop_without_raising():
    async def scenario() -> None:
        wakeup = ManualWakeup()
        planificateur = Planificateur(
            store=ScriptedStore(),
            now=lambda: NOW,
            timezone=PARIS,
            sleep=asyncio.sleep,
            emit=lambda occurrence: None,
            trace=lambda kind, payload: None,
            wake=wakeup,
        )
        task = asyncio.create_task(planificateur.run())
        await wait_until(lambda: bool(wakeup.waits))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()

    asyncio.run(scenario())
