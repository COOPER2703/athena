"""Bandeau de santé et tailles de bases, unitairement (ticket #177).

Le noyau n'assemble que des faits injectés : l'uptime, la dernière
Consolidation, la dernière erreur et les tailles sont fournis par la couche de
composition. Ces tests fixent la forme publiée et les cas limites (base
absente, Consolidation vide, Trace non erreur).
"""

from __future__ import annotations

import json
from pathlib import Path

from server.app.health import database_sizes
from server.core.consolidation import ConsolidationReport
from server.core.health import HealthState, health_snapshot
from server.core.ports import TraceEntry


class _Clock:
    """Horloge murale de test : figée jusqu'à ``set``."""

    def __init__(self, value: float = 1_700_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _entry(level: str, *, source: str = "gemini", kind: str = "LlmFailed") -> TraceEntry:
    return TraceEntry(
        timestamp=1_700_000_123.5,
        source=source,
        kind=kind,
        payload={"message": "secret-interdit", "key": "ath_deadbeef"},
        level=level,
    )


def _snapshot(state: HealthState, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "version": "9.9.9",
        "uptime_seconds": 12.5,
        "state": state,
        "databases": {"memory": 10, "auth": 20, "settings": 0, "schedule": 0},
    }
    values.update(overrides)
    return health_snapshot(**values)  # type: ignore[arg-type]


def test_a_fresh_health_section_has_no_error_and_no_consolidation() -> None:
    section = _snapshot(HealthState(now=_Clock()))

    assert section == {
        "version": "9.9.9",
        "uptime_seconds": 12.5,
        "last_consolidation": None,
        "databases": {"memory": 10, "auth": 20, "settings": 0, "schedule": 0},
        "last_error": None,
        "trace_buffer": None,
    }


def test_the_last_error_never_carries_the_trace_payload() -> None:
    state = HealthState(now=_Clock())
    state.emit(_entry("info"))
    assert state.last_error is None

    state.emit(_entry("error", source="consolidation", kind="ConsolidationFailed"))

    assert state.last_error is not None
    assert state.last_error.source == "consolidation"
    assert state.last_error.kind == "ConsolidationFailed"
    serialized = json.dumps(_snapshot(state), ensure_ascii=False)
    assert "secret-interdit" not in serialized
    assert "ath_deadbeef" not in serialized


def test_the_last_error_keeps_the_most_recent_one() -> None:
    state = HealthState(now=_Clock())
    state.emit(_entry("error", source="first"))
    state.emit(_entry("critical", source="second"))

    assert state.last_error is not None
    assert state.last_error.source == "second"
    assert _snapshot(state)["last_error"] == {
        "timestamp": 1_700_000_123.5,
        "source": "second",
        "kind": "LlmFailed",
        "level": "critical",
    }


def test_a_consolidation_that_did_not_run_is_not_reported() -> None:
    clock = _Clock()
    state = HealthState(now=clock)

    state.record_consolidation(ConsolidationReport(ran=False))
    assert _snapshot(state)["last_consolidation"] is None

    clock.value = 1_700_000_500.0
    state.record_consolidation(
        ConsolidationReport(
            ran=True,
            facts_extracted=4,
            facts_stored=3,
            duplicates=1,
            purged=2,
            transcript_chars=800,
        )
    )

    assert _snapshot(state)["last_consolidation"] == {
        "at": 1_700_000_500.0,
        "facts_extracted": 4,
        "facts_stored": 3,
        "duplicates": 1,
        "purged": 2,
        "transcript_chars": 800,
    }


def test_sizes_and_uptime_are_bounded_and_ordered() -> None:
    section = _snapshot(
        HealthState(now=_Clock()),
        uptime_seconds=-5.0,
        databases={"auth": -3, "memory": 1},
        trace_buffer={"size": 7, "capacity": 500},
    )

    assert section["uptime_seconds"] == 0.0
    # Les quatre bases sont toujours publiées, dans l'ordre stable, jamais négatif.
    assert section["databases"] == {
        "memory": 1,
        "auth": 0,
        "settings": 0,
        "schedule": 0,
    }
    assert section["trace_buffer"] == {"size": 7, "capacity": 500}


def test_database_sizes_sum_the_wal_and_shared_memory_files(
    tmp_path: Path,
) -> None:
    memory_db = tmp_path / "memory.db"
    auth_db = tmp_path / "auth.db"
    memory_db.write_bytes(b"a" * 10)
    Path(f"{memory_db}-wal").write_bytes(b"b" * 5)
    Path(f"{memory_db}-shm").write_bytes(b"c" * 2)
    auth_db.write_bytes(b"d" * 3)

    sizes = database_sizes(
        {
            "memory": memory_db,
            "auth": auth_db,
            "settings": tmp_path / "settings.db",
            "schedule": tmp_path / "schedule.db",
        }
    )

    assert sizes == {"memory": 17, "auth": 3, "settings": 0, "schedule": 0}
