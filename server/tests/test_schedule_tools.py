"""Comportement des Tools du noyau ``create_schedule`` et ``list_schedules``.

Couture Tools : on appelle le registre comme le fait le modèle et on observe
les charges rendues (statut, identifiant, prochaine occurrence, règle). Aucun
test ne touche au SQL ni aux fonctions privées ; la persistance est observée par
un second store sur la même base.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from server.adapters.schedule import SqliteScheduleStore
from server.core.ports import Schedule, ScheduleRule
from server.core.schedule_rules import MAX_CONTENT_CHARS, MAX_SCHEDULES
from server.core.schedule_tools import register_schedule_tools
from server.core.tools import ToolRegistry

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 1, 1, 10, 0, 0, tzinfo=PARIS)
ID_PATTERN = re.compile(r"^sch_[0-9a-f]{32}$")


def _clock(moment: datetime = NOW):
    return lambda: moment


def _registry(
    tmp_path: Path,
    *,
    moment: datetime = NOW,
    on_change=None,
    db_name: str = "schedule.db",
) -> tuple[ToolRegistry, SqliteScheduleStore]:
    store = SqliteScheduleStore(db_path=str(tmp_path / db_name))
    registry = ToolRegistry()
    register_schedule_tools(
        registry,
        store,
        now=_clock(moment),
        timezone=PARIS,
        on_change=on_change,
    )
    return registry, store


def _execute(registry: ToolRegistry, name: str, arguments: dict) -> dict:
    result = asyncio.run(registry.execute(name, arguments))
    return dict(result)


def test_the_two_tools_have_their_stable_names_and_parameters(
    tmp_path: Path,
) -> None:
    registry, _ = _registry(tmp_path)

    assert {spec.name for spec in registry.specs()} == {
        "create_schedule",
        "list_schedules",
    }
    assert registry.owner_of("create_schedule") == "core"
    assert registry.owner_of("list_schedules") == "core"

    create = next(
        spec for spec in registry.specs() if spec.name == "create_schedule"
    )
    assert create.parameters["required"] == ["content", "when"]
    assert set(create.parameters["properties"]) == {"content", "when"}
    when = create.parameters["properties"]["when"]
    assert when["properties"]["kind"]["enum"] == [
        "once",
        "interval",
        "daily",
        "weekly",
    ]

    listed = next(
        spec for spec in registry.specs() if spec.name == "list_schedules"
    )
    assert listed.parameters == {"type": "object", "properties": {}}


def test_create_schedule_records_a_relative_reminder(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)

    created = _execute(
        registry,
        "create_schedule",
        {"content": "  appeler le plombier  ", "when": {"kind": "once", "delay_seconds": 600}},
    )

    assert created["status"] == "ok"
    assert ID_PATTERN.match(str(created["id"]))
    assert created["next_occurrence"] == "2026-01-01T10:10:00+01:00"


def test_list_schedules_renders_id_content_rule_and_next_occurrence(
    tmp_path: Path,
) -> None:
    registry, _ = _registry(tmp_path)
    created = _execute(
        registry,
        "create_schedule",
        {"content": "point hebdo", "when": {
            "kind": "weekly", "days": [1, 3, 5], "time": "09:00"
        }},
    )

    listed = _execute(registry, "list_schedules", {})

    assert listed["status"] == "ok"
    assert listed["schedules"] == [
        {
            "id": created["id"],
            "content": "point hebdo",
            "rule": {"kind": "weekly", "days": [1, 3, 5], "time": "09:00"},
            "next_occurrence": "2026-01-02T09:00:00+01:00",
        }
    ]


def test_an_empty_list_is_a_success(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)

    assert _execute(registry, "list_schedules", {}) == {
        "status": "ok",
        "schedules": [],
    }


def test_a_schedule_survives_a_restart_through_a_second_registry(
    tmp_path: Path,
) -> None:
    first, _ = _registry(tmp_path)
    created = _execute(
        first,
        "create_schedule",
        {"content": "sortir les poubelles", "when": {"kind": "daily", "time": "20:00"}},
    )

    second, _ = _registry(tmp_path)
    listed = _execute(second, "list_schedules", {})

    assert [item["id"] for item in listed["schedules"]] == [created["id"]]
    assert listed["schedules"][0]["content"] == "sortir les poubelles"


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        (
            {"content": "", "when": {"kind": "daily", "time": "08:00"}},
            "content",
        ),
        (
            {
                "content": "x" * (MAX_CONTENT_CHARS + 1),
                "when": {"kind": "daily", "time": "08:00"},
            },
            "4096",
        ),
        (
            {"content": "rappel", "when": {"kind": "cron"}},
            "when",
        ),
        (
            {"content": "rappel", "when": {"kind": "interval", "every_seconds": 59}},
            "60",
        ),
        (
            {"content": "rappel", "when": {"kind": "weekly", "days": [], "time": "09:00"}},
            "days",
        ),
        (
            {"content": "rappel", "when": {"kind": "once", "delay_seconds": 0}},
            "future",
        ),
        (
            {"content": "rappel", "when": {"kind": "once", "at": "2025-12-31T10:00:00"}},
            "future",
        ),
        (
            {"content": "rappel", "when": {"kind": "once", "at": "2026-02-30T10:00:00"}},
            "when",
        ),
        ("not an object", "object"),
    ],
)
def test_every_invalid_input_is_a_readable_error_and_never_raises(
    tmp_path: Path, arguments: object, fragment: str
) -> None:
    registry, store = _registry(tmp_path)

    result = _execute(registry, "create_schedule", arguments)  # type: ignore[arg-type]

    assert result["status"] == "error"
    assert fragment in result["message"].lower()
    assert store.count() == 0


def test_the_two_hundred_schedule_cap_is_a_readable_error(
    tmp_path: Path,
) -> None:
    registry, store = _registry(tmp_path)
    for index in range(MAX_SCHEDULES):
        store.create(
            Schedule(
                id=f"sch_{index:032x}",
                content="déjà là",
                rule=ScheduleRule(kind="daily", time_of_day="08:00"),
                next_occurrence=1_700_000_000 + index,
                created_at=1_700_000_000,
                updated_at=1_700_000_000,
            )
        )

    result = _execute(
        registry,
        "create_schedule",
        {"content": "un de trop", "when": {"kind": "daily", "time": "08:00"}},
    )

    assert result["status"] == "error"
    assert str(MAX_SCHEDULES) in result["message"]
    assert store.count() == MAX_SCHEDULES


def test_on_change_runs_once_after_a_successful_create_only(
    tmp_path: Path,
) -> None:
    changes: list[int] = []
    registry, _ = _registry(tmp_path, on_change=lambda: changes.append(1))

    failed = _execute(registry, "create_schedule", {"content": "", "when": {
        "kind": "daily", "time": "08:00"
    }})
    assert failed["status"] == "error"
    assert changes == []

    created = _execute(
        registry,
        "create_schedule",
        {"content": "rappel", "when": {"kind": "daily", "time": "08:00"}},
    )
    assert created["status"] == "ok"
    assert changes == [1]

    # Une lecture ne réveille personne.
    _execute(registry, "list_schedules", {})
    assert changes == [1]


def test_a_once_rule_is_stored_as_its_absolute_epoch(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)

    created = _execute(
        registry,
        "create_schedule",
        {"content": "demain matin", "when": {"kind": "once", "at": "2026-01-02T08:30:00"}},
    )
    listed = _execute(registry, "list_schedules", {})

    assert created["next_occurrence"] == "2026-01-02T08:30:00+01:00"
    assert listed["schedules"][0]["rule"] == {
        "kind": "once",
        "at": 1767339000,
    }


def test_the_tool_accepts_a_timezone_name(tmp_path: Path) -> None:
    store = SqliteScheduleStore(db_path=str(tmp_path / "schedule.db"))
    registry = ToolRegistry()
    register_schedule_tools(
        registry, store, now=_clock(NOW), timezone="Europe/Paris"
    )

    created = _execute(
        registry,
        "create_schedule",
        {"content": "rappel", "when": {"kind": "daily", "time": "08:00"}},
    )

    assert created["status"] == "ok"
