"""Comportement du magasin SQLite des Planifications (couture basse).

Ces tests parlent au port ``ScheduleStore`` et observent des effets externes :
une Planification relue après un redémarrage, un ordre stable, un plafond. Le
calcul des règles appartient à ``schedule_rules`` et n'est pas retesté ici.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from server.adapters.schedule import SqliteScheduleStore
from server.core.ports import Schedule, ScheduleError, ScheduleRule
from server.core.schedule_rules import MAX_SCHEDULES
from testkit import RecordingTraceSink


def _store(
    tmp_path: Path,
    *,
    sink: RecordingTraceSink | None = None,
    name: str = "schedule.db",
) -> SqliteScheduleStore:
    return SqliteScheduleStore(
        db_path=str(tmp_path / name),
        trace_sink=sink,
    )


def _schedule(
    *,
    schedule_id: str,
    next_occurrence: int,
    content: str = "appeler le plombier",
    rule: ScheduleRule | None = None,
    created_at: int = 1_700_000_000,
) -> Schedule:
    return Schedule(
        id=schedule_id,
        content=content,
        rule=rule or ScheduleRule(kind="once", at=next_occurrence),
        next_occurrence=next_occurrence,
        created_at=created_at,
        updated_at=created_at,
    )


def test_a_schedule_survives_a_new_store_on_the_same_database(
    tmp_path: Path,
) -> None:
    first = _store(tmp_path)
    stored = _schedule(
        schedule_id="sch_abc",
        next_occurrence=1_700_000_600,
        rule=ScheduleRule(kind="interval", every_seconds=600),
    )
    assert first.create(stored) == stored

    second = _store(tmp_path)

    assert second.count() == 1
    assert second.get("sch_abc") == stored
    assert second.list() == (stored,)


def test_an_unknown_id_has_no_schedule(tmp_path: Path) -> None:
    assert _store(tmp_path).get("sch_missing") is None


def test_schedules_are_listed_by_imminent_next_occurrence(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    late = _schedule(schedule_id="sch_late", next_occurrence=1_700_000_900)
    early = _schedule(schedule_id="sch_early", next_occurrence=1_700_000_100)
    middle = _schedule(schedule_id="sch_middle", next_occurrence=1_700_000_500)

    for schedule in (late, middle, early):
        store.create(schedule)

    assert [item.id for item in store.list()] == [
        "sch_early",
        "sch_middle",
        "sch_late",
    ]
    assert store.count() == 3


def test_the_stored_rule_is_json_and_the_instant_is_an_integer(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    rule = ScheduleRule(
        kind="weekly", time_of_day="09:00", days=(1, 3, 5)
    )
    store.create(
        _schedule(schedule_id="sch_week", next_occurrence=1_700_000_000, rule=rule)
    )

    with sqlite3.connect(tmp_path / "schedule.db") as db:
        row = db.execute(
            "SELECT next_occurrence, rule_json FROM schedules WHERE id = ?",
            ("sch_week",),
        ).fetchone()

    assert row is not None
    assert row[0] == 1_700_000_000
    assert isinstance(row[0], int)
    assert '"weekly"' in row[1] and '"09:00"' in row[1]


def test_the_cap_of_two_hundred_schedules_is_applied_at_insertion(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    for index in range(MAX_SCHEDULES):
        store.create(
            _schedule(
                schedule_id=f"sch_{index:03d}",
                next_occurrence=1_700_000_000 + index,
            )
        )

    with pytest.raises(ScheduleError) as excinfo:
        store.create(
            _schedule(schedule_id="sch_overflow", next_occurrence=1_700_100_000)
        )

    assert excinfo.value.code == "too_many_schedules"
    assert store.count() == MAX_SCHEDULES


def test_the_store_never_creates_a_parent_directory(tmp_path: Path) -> None:
    store = _store(tmp_path, name="absent/schedule.db")

    with pytest.raises(sqlite3.OperationalError):
        store.create(_schedule(schedule_id="sch_x", next_occurrence=1_700_000_000))

    assert not (tmp_path / "absent").exists()


def test_creating_a_schedule_emits_a_trace(tmp_path: Path) -> None:
    sink = RecordingTraceSink()
    store = _store(tmp_path, sink=sink)

    store.create(
        _schedule(
            schedule_id="sch_trace",
            next_occurrence=1_700_000_000,
            rule=ScheduleRule(kind="daily", time_of_day="08:00"),
        )
    )

    created = sink.of_kind("ScheduleCreated")
    assert len(created) == 1
    assert created[0].source == "schedule"
    assert created[0].payload["id"] == "sch_trace"
    assert created[0].payload["kind"] == "daily"
