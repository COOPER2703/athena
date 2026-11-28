"""Réclamation des occurrences dues par le magasin (ADR-0028, #195/#196).

Couture basse du ``ScheduleStore`` : ``claim_due`` rend, dans **une** section
critique, les Planifications dont l'échéance est atteinte. Une ponctuelle est
effacée et rendue telle qu'elle était à l'échéance ; une récurrente est avancée
au prochain créneau valide et rendue. Au démarrage, une récurrente en retard est
avancée **sans** être rendue (pas de rafale), tandis qu'une ponctuelle est
rattrapée une fois. Une récurrence par intervalle garde sa phase et un créneau
local sauté ou doublé par le changement d'heure n'est jamais rejoué.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from server.adapters.schedule import SqliteScheduleStore
from server.core.ports import Schedule, ScheduleRule
from testkit import RecordingTraceSink


def _store(tmp_path: Path, *, name: str = "schedule.db") -> SqliteScheduleStore:
    return SqliteScheduleStore(
        db_path=str(tmp_path / name), trace_sink=RecordingTraceSink()
    )


def _schedule(
    *,
    schedule_id: str,
    next_occurrence: int,
    rule: ScheduleRule | None = None,
    content: str = "appeler le plombier",
) -> Schedule:
    return Schedule(
        id=schedule_id,
        content=content,
        rule=rule or ScheduleRule(kind="once", at=next_occurrence),
        next_occurrence=next_occurrence,
        created_at=1_700_000_000,
        updated_at=1_700_000_000,
    )


def _epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp())


NOW = _epoch("2026-06-01T12:00:00+00:00")
PARIS = ZoneInfo("Europe/Paris")


def test_a_due_one_shot_is_returned_as_it_was_and_deleted(tmp_path: Path) -> None:
    store = _store(tmp_path)
    due = _schedule(schedule_id="sch_due", next_occurrence=NOW - 60)
    later = _schedule(schedule_id="sch_later", next_occurrence=NOW + 600)
    store.create(due)
    store.create(later)

    claimed = store.claim_due(NOW, timezone=PARIS)

    assert claimed == (due,)
    assert store.get("sch_due") is None
    assert store.get("sch_later") == later


def test_nothing_due_claims_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.create(_schedule(schedule_id="sch_later", next_occurrence=NOW + 1))

    assert store.claim_due(NOW, timezone=PARIS) == ()


def test_a_due_recurring_schedule_is_advanced_and_returned(tmp_path: Path) -> None:
    store = _store(tmp_path)
    at = NOW - 120
    recurring = _schedule(
        schedule_id="sch_tick",
        next_occurrence=at,
        rule=ScheduleRule(kind="interval", every_seconds=60),
    )
    store.create(recurring)

    claimed = store.claim_due(NOW, timezone=PARIS)

    # Rendue telle qu'elle était à l'échéance : la Planificateur émet le
    # contenu réellement dû.
    assert claimed == (recurring,)
    stored = store.get("sch_tick")
    assert stored is not None
    assert stored.next_occurrence == NOW + 60
    assert stored.rule == recurring.rule


def test_a_daily_rule_advances_to_the_next_local_slot_in_the_instance_timezone(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    # 9 h à Paris = 7 h UTC ; l'échéance d'hier est due à 12 h UTC.
    yesterday = _epoch("2026-05-31T07:00:00+00:00")
    daily = _schedule(
        schedule_id="sch_daily",
        next_occurrence=yesterday,
        rule=ScheduleRule(kind="daily", time_of_day="09:00"),
    )
    store.create(daily)

    store.claim_due(NOW, timezone=PARIS)

    stored = store.get("sch_daily")
    assert stored is not None
    assert stored.next_occurrence == _epoch("2026-06-02T07:00:00+00:00")


def test_an_interval_advances_from_its_previous_slot_not_from_now(
    tmp_path: Path,
) -> None:
    """Une boucle en retard de quelques secondes ne dérive pas la récurrence."""
    store = _store(tmp_path)
    previous = NOW - 3
    tick = _schedule(
        schedule_id="sch_tick",
        next_occurrence=previous,
        rule=ScheduleRule(kind="interval", every_seconds=600),
    )
    store.create(tick)

    claimed = store.claim_due(NOW, timezone=PARIS)

    assert claimed == (tick,)
    stored = store.get("sch_tick")
    assert stored is not None
    # 3 s de retard : la prochaine est l'échéance prévue, pas ``now + 600``.
    assert stored.next_occurrence == previous + 600


def test_an_interval_late_by_several_periods_resumes_at_the_next_slot(
    tmp_path: Path,
) -> None:
    """Plusieurs périodes ratées : un seul créneau repris, jamais de rafale."""
    store = _store(tmp_path)
    every = 600
    previous = NOW - 3 * every - 30
    tick = _schedule(
        schedule_id="sch_tick",
        next_occurrence=previous,
        rule=ScheduleRule(kind="interval", every_seconds=every),
    )
    store.create(tick)

    claimed = store.claim_due(NOW, timezone=PARIS)

    assert claimed == (tick,)
    stored = store.get("sch_tick")
    assert stored is not None
    assert stored.next_occurrence == previous + 4 * every
    assert stored.next_occurrence > NOW


def test_at_startup_an_interval_keeps_its_phase_across_many_missed_slots(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    every = 600
    previous = NOW - 3 * every - 30
    tick = _schedule(
        schedule_id="sch_tick",
        next_occurrence=previous,
        rule=ScheduleRule(kind="interval", every_seconds=every),
    )
    store.create(tick)

    claimed = store.claim_due(NOW, timezone=PARIS, startup=True)

    assert claimed == ()
    stored = store.get("sch_tick")
    assert stored is not None
    assert stored.next_occurrence == previous + 4 * every
    assert stored.next_occurrence > NOW


def test_at_startup_a_missed_recurring_schedule_is_advanced_without_being_returned(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    missed = _schedule(
        schedule_id="sch_tick",
        next_occurrence=NOW - 3600,
        rule=ScheduleRule(kind="interval", every_seconds=60),
    )
    store.create(missed)

    claimed = store.claim_due(NOW, timezone=PARIS, startup=True)

    assert claimed == ()
    stored = store.get("sch_tick")
    assert stored is not None
    assert stored.next_occurrence == NOW + 60


def test_at_startup_a_missed_one_shot_is_caught_up_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    missed = _schedule(schedule_id="sch_once", next_occurrence=NOW - 3600)
    store.create(missed)

    claimed = store.claim_due(NOW, timezone=PARIS, startup=True)

    assert claimed == (missed,)
    assert store.get("sch_once") is None


def test_a_daily_slot_skipped_by_spring_forward_advances_without_duplicate(
    tmp_path: Path,
) -> None:
    """29 mars 2026 : 02:30 n'existe pas à Paris, aucun doublon."""
    store = _store(tmp_path)
    due = _epoch("2026-03-28T01:30:00+00:00")
    daily = _schedule(
        schedule_id="sch_daily",
        next_occurrence=due,
        rule=ScheduleRule(kind="daily", time_of_day="02:30"),
    )
    store.create(daily)

    now = _epoch("2026-03-29T01:00:00+00:00")  # 03:00 CEST, après le créneau inexistant
    claimed = store.claim_due(now, timezone=PARIS)

    assert claimed == (daily,)
    stored = store.get("sch_daily")
    assert stored is not None
    # Prochain créneau valide : lundi 30 mars 02:30 CEST (00:30 UTC).
    assert stored.next_occurrence == _epoch("2026-03-30T00:30:00+00:00")


def test_at_startup_a_daily_slot_skipped_by_spring_forward_advances_once(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    daily = _schedule(
        schedule_id="sch_daily",
        next_occurrence=_epoch("2026-03-28T01:30:00+00:00"),
        rule=ScheduleRule(kind="daily", time_of_day="02:30"),
    )
    store.create(daily)

    claimed = store.claim_due(
        _epoch("2026-03-29T01:00:00+00:00"), timezone=PARIS, startup=True
    )

    assert claimed == ()
    stored = store.get("sch_daily")
    assert stored is not None
    assert stored.next_occurrence == _epoch("2026-03-30T00:30:00+00:00")


def test_a_doubled_local_slot_is_emitted_once_then_waits_for_the_next_day(
    tmp_path: Path,
) -> None:
    """25 octobre 2026 : 02:30 existe deux fois ; la seconde n'est pas rejouée."""
    store = _store(tmp_path)
    daily = _schedule(
        schedule_id="sch_daily",
        next_occurrence=_epoch("2026-10-24T00:30:00+00:00"),
        rule=ScheduleRule(kind="daily", time_of_day="02:30"),
    )
    store.create(daily)

    # Avant la première occurrence (02:30 CEST = 00:30 UTC).
    claimed = store.claim_due(
        _epoch("2026-10-25T00:00:00+00:00"), timezone=PARIS
    )
    assert claimed == (daily,)
    stored = store.get("sch_daily")
    assert stored is not None
    assert stored.next_occurrence == _epoch("2026-10-25T00:30:00+00:00")

    # Exactement à la première occurrence : la suivante est le lendemain, pas
    # la seconde 02:30 (01:30 UTC).
    claimed = store.claim_due(stored.next_occurrence, timezone=PARIS)
    assert claimed == (stored,)
    stored = store.get("sch_daily")
    assert stored is not None
    assert stored.next_occurrence == _epoch("2026-10-26T01:30:00+00:00")


def test_a_weekly_slot_skipped_by_spring_forward_walks_to_the_next_week(
    tmp_path: Path,
) -> None:
    """Dimanche 29 mars 2026 : 02:30 n'existe pas, le créneau passe au 5 avril."""
    store = _store(tmp_path)
    weekly = _schedule(
        schedule_id="sch_week",
        next_occurrence=_epoch("2026-03-22T01:30:00+00:00"),
        rule=ScheduleRule(kind="weekly", days=(7,), time_of_day="02:30"),
    )
    store.create(weekly)

    claimed = store.claim_due(
        _epoch("2026-03-29T01:00:00+00:00"), timezone=PARIS
    )

    assert claimed == (weekly,)
    stored = store.get("sch_week")
    assert stored is not None
    assert stored.next_occurrence == _epoch("2026-04-05T00:30:00+00:00")


def test_a_weekly_doubled_slot_is_emitted_once_then_waits_for_the_next_week(
    tmp_path: Path,
) -> None:
    """Dimanche 25 octobre 2026 : 02:30 existe deux fois, une seule Annonce."""
    store = _store(tmp_path)
    weekly = _schedule(
        schedule_id="sch_week",
        next_occurrence=_epoch("2026-10-18T00:30:00+00:00"),
        rule=ScheduleRule(kind="weekly", days=(7,), time_of_day="02:30"),
    )
    store.create(weekly)

    claimed = store.claim_due(
        _epoch("2026-10-25T00:00:00+00:00"), timezone=PARIS
    )

    assert claimed == (weekly,)
    stored = store.get("sch_week")
    assert stored is not None
    assert stored.next_occurrence == _epoch("2026-10-25T00:30:00+00:00")

    claimed = store.claim_due(stored.next_occurrence, timezone=PARIS)
    assert claimed == (stored,)
    stored = store.get("sch_week")
    assert stored is not None
    assert stored.next_occurrence == _epoch("2026-11-01T01:30:00+00:00")


def test_at_startup_a_future_recurring_schedule_is_left_untouched(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    future = _schedule(
        schedule_id="sch_future",
        next_occurrence=NOW + 600,
        rule=ScheduleRule(kind="daily", time_of_day="09:00"),
    )
    store.create(future)

    assert store.claim_due(NOW, timezone=PARIS, startup=True) == ()
    assert store.get("sch_future") == future


def test_a_failure_in_the_middle_rolls_back_the_whole_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    once = _schedule(schedule_id="sch_once", next_occurrence=NOW - 120)
    recurring = _schedule(
        schedule_id="sch_tick",
        next_occurrence=NOW - 60,
        rule=ScheduleRule(kind="interval", every_seconds=60),
    )
    store.create(once)
    store.create(recurring)

    def boom(*args, **kwargs):
        raise RuntimeError("règle corrompue")

    monkeypatch.setattr("server.adapters.schedule.store.advance_occurrence", boom)

    with pytest.raises(RuntimeError):
        store.claim_due(NOW, timezone=PARIS)

    # La suppression de la ponctuelle n'a pas été validée : la transaction est
    # entièrement annulée.
    assert store.get("sch_once") == once
    assert store.get("sch_tick") == recurring


def test_claims_preserve_arrival_order_by_imminent_due_date(tmp_path: Path) -> None:
    store = _store(tmp_path)
    later = _schedule(schedule_id="sch_late", next_occurrence=NOW - 10)
    earlier = _schedule(schedule_id="sch_early", next_occurrence=NOW - 20)
    store.create(later)
    store.create(earlier)

    claimed = store.claim_due(NOW, timezone=PARIS)

    assert [schedule.id for schedule in claimed] == ["sch_early", "sch_late"]
