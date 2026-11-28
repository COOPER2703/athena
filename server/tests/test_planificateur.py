"""Boucle du Planificateur (ADR-0028, #195/#196), sans horloge ni sommeil réels.

La couture est ``Planificateur.run`` : magasin, ``now``, fusion du sommeil par
``Wakeup``, émission et traces sont injectés. On observe ce que la boucle
**émet** et **trace** — jamais sa structure interne. Les tests de récurrence et
de changement d'heure composent la boucle avec le vrai magasin SQLite.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from testkit import wait_until
from server.adapters.schedule import SqliteScheduleStore
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
        self.startup_results: list[tuple[Schedule, ...]] = []

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
            result = self.startup_results.pop(0) if self.startup_results else ()
        else:
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
            # Une seule Trace pour la Planification, jamais une par créneau
            # manqué (ici dix périodes) : ``due`` et ``now`` disent l'écart.
            assert skipped == [
                {
                    "schedule_id": "sch_tick",
                    "kind": "interval",
                    "reason": "missed_slots",
                    "due": int(NOW.timestamp()) - 600,
                    "now": int(NOW.timestamp()),
                }
            ]
            # Le rattrapage de démarrage est réclamé avec ``startup=True``.
            assert store.claims[0][1] is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_future_recurring_schedule_is_untouched_at_startup():
    async def scenario() -> None:
        future = _schedule(
            schedule_id="sch_future",
            next_occurrence=int(NOW.timestamp()) + 300,
            rule=ScheduleRule(kind="daily", time_of_day="09:00"),
        )
        store = ScriptedStore([future])
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
            assert [kind for kind, _ in traces] == []
            assert store.list() == (future,)
            assert wakeup.waits[-1] == 300.0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_a_once_due_at_startup_is_caught_up_once_without_a_skip_trace():
    async def scenario() -> None:
        due = _schedule(
            schedule_id="sch_once",
            next_occurrence=int(NOW.timestamp()) - 7200,
            rule=ScheduleRule(kind="once", at=int(NOW.timestamp()) - 7200),
            content="sortir les poubelles",
        )
        store = ScriptedStore([])
        store.startup_results = [(due,)]
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
                    content="sortir les poubelles", schedule_id="sch_once"
                )
            ]
            assert [
                kind for kind, _ in traces if kind == "OccurrenceSkipped"
            ] == []
            assert [kind for kind, _ in traces] == ["OccurrenceEmitted"]
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


def test_the_pump_emits_an_interval_on_its_phase_and_waits_for_the_exact_delay(
    tmp_path,
) -> None:
    """Boucle en retard de 3 s : l'échéance suivante reste ``previous + 600``."""
    store = SqliteScheduleStore(db_path=str(tmp_path / "schedule.db"))

    async def scenario() -> None:
        clock = [NOW]

        emit: list[OccurrenceReceived] = []
        wakeup = ManualWakeup()
        planificateur = Planificateur(
            store=store,
            now=lambda: clock[0],
            timezone=PARIS,
            sleep=asyncio.sleep,
            emit=emit.append,
            trace=lambda kind, payload: None,
            wake=wakeup,
        )

        task = await _run_until_first_wait(planificateur, wakeup)
        try:
            # Aucune Planification au démarrage : le pump attend un réveil.
            assert wakeup.waits[-1] is None
            due = int(NOW.timestamp())
            store.create(
                _schedule(
                    schedule_id="sch_tick",
                    next_occurrence=due,
                    rule=ScheduleRule(kind="interval", every_seconds=600),
                )
            )
            # La boucle se réveille 3 s après le créneau : elle émet une fois.
            clock[0] = NOW.replace(microsecond=0) + timedelta(seconds=3)
            wakeup.notify()
            await wait_until(lambda: bool(emit))
            await wait_until(lambda: wakeup.waits[-1] == 597.0)
            assert [occurrence.schedule_id for occurrence in emit] == ["sch_tick"]
            stored = store.get("sch_tick")
            assert stored is not None
            assert stored.next_occurrence == due + 600
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_the_pump_skips_the_missing_spring_forward_slot_without_duplicate(
    tmp_path,
) -> None:
    """02:30 n'existe pas le 29 mars 2026 : une Annonce le 28, la suivante le 30."""
    store = SqliteScheduleStore(db_path=str(tmp_path / "schedule.db"))

    async def scenario() -> None:
        clock = [datetime(2026, 3, 28, 1, 0, tzinfo=timezone.utc)]
        emit: list[OccurrenceReceived] = []
        wakeup = ManualWakeup()
        planificateur = Planificateur(
            store=store,
            now=lambda: clock[0],
            timezone=PARIS,
            sleep=asyncio.sleep,
            emit=emit.append,
            trace=lambda kind, payload: None,
            wake=wakeup,
        )

        task = await _run_until_first_wait(planificateur, wakeup)
        try:
            due = int(
                datetime(2026, 3, 28, 1, 30, tzinfo=timezone.utc).timestamp()
            )
            store.create(
                _schedule(
                    schedule_id="sch_daily",
                    next_occurrence=due,
                    rule=ScheduleRule(kind="daily", time_of_day="02:30"),
                )
            )
            wakeup.notify()
            await wait_until(lambda: wakeup.waits[-1] == 1800.0)
            assert emit == []

            clock[0] = datetime(2026, 3, 28, 1, 30, tzinfo=timezone.utc)
            wakeup.release()
            await wait_until(lambda: len(emit) == 1)
            # Le 29 mars saute : le prochain créneau valide est le 30 à 02:30 CEST.
            await wait_until(lambda: wakeup.waits[-1] == 169_200.0)
            stored = store.get("sch_daily")
            assert stored is not None
            assert stored.next_occurrence == int(
                datetime(2026, 3, 30, 0, 30, tzinfo=timezone.utc).timestamp()
            )
            assert len(emit) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_the_pump_emits_a_doubled_autumn_slot_once(tmp_path) -> None:
    """02:30 existe deux fois le 25 octobre 2026 : la seconde ne rejoue rien."""
    store = SqliteScheduleStore(db_path=str(tmp_path / "schedule.db"))

    async def scenario() -> None:
        clock = [datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)]
        emit: list[OccurrenceReceived] = []
        wakeup = ManualWakeup()
        planificateur = Planificateur(
            store=store,
            now=lambda: clock[0],
            timezone=PARIS,
            sleep=asyncio.sleep,
            emit=emit.append,
            trace=lambda kind, payload: None,
            wake=wakeup,
        )

        task = await _run_until_first_wait(planificateur, wakeup)
        try:
            first = int(
                datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()
            )
            store.create(
                _schedule(
                    schedule_id="sch_daily",
                    next_occurrence=first,
                    rule=ScheduleRule(kind="daily", time_of_day="02:30"),
                )
            )
            wakeup.notify()
            await wait_until(lambda: wakeup.waits[-1] == 45_000.0)

            clock[0] = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
            wakeup.release()
            await wait_until(lambda: len(emit) == 1)
            await wait_until(lambda: wakeup.waits[-1] == 90_000.0)

            # Seconde 02:30 (01:30 UTC) : un réveil ne doit rien réémettre.
            clock[0] = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)
            wakeup.release()
            await wait_until(lambda: wakeup.waits[-1] == 86_400.0)
            assert len(emit) == 1

            # Le lendemain, la récurrence reprend normalement.
            clock[0] = datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc)
            wakeup.release()
            await wait_until(lambda: len(emit) == 2)
            stored = store.get("sch_daily")
            assert stored is not None
            assert stored.next_occurrence == int(
                datetime(2026, 10, 27, 1, 30, tzinfo=timezone.utc).timestamp()
            )
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
