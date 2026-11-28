"""Réclamation des occurrences dues par le magasin (ADR-0028, #195).

Couture basse du ``ScheduleStore`` : ``claim_due`` rend, dans **une** section
critique, les Planifications dont l'échéance est atteinte. Une ponctuelle est
effacée et rendue telle qu'elle était à l'échéance ; une récurrente est avancée
au prochain créneau valide et rendue. Au démarrage, une récurrente en retard est
avancée **sans** être rendue (pas de rafale), tandis qu'une ponctuelle est
rattrapée une fois.
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

    monkeypatch.setattr("server.adapters.schedule.store.next_occurrence", boom)

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
