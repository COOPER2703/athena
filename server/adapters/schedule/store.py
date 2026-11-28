"""Magasin SQLite des Planifications (ADR-0028).

Une base **dédiée** (``ATHENA_SCHEDULE_DB``, ``schedule.db`` par défaut), sur le
modèle de ``auth.db`` : schéma créé par ``CREATE TABLE IF NOT EXISTS``, aucune
migration, connexion paresseuse. Les instants sont des entiers UTC (epoch
secondes), les règles des objets JSON canoniques (``schedule_rules``).

Le répertoire parent n'est jamais créé : un déploiement sans ``/data`` doit
échouer franchement plutôt que d'écrire dans un chemin inattendu. Chaque
écriture est protégée par un verrou et suivie d'un ``commit`` ; le plafond de
200 Planifications est vérifié dans la même section critique que l'insertion.

Comme les autres adaptateurs, ce module n'importe pas ``logging`` : création,
modification et suppression passent par le ``TraceSink`` injecté, donc par le
flux unique de traces.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from server.adapters.trace import NullTraceSink, emit
from server.core.ports import Schedule, ScheduleError, ScheduleRule, TraceSink
from server.core.schedule_rules import (
    MAX_SCHEDULES,
    advance_occurrence,
    rule_from_json,
    rule_to_json,
)

TRACE_SOURCE = "schedule"

_SCHEDULES_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id              TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    rule_json       TEXT NOT NULL,
    next_occurrence INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
)
"""

_SCHEDULES_INDEX = """
CREATE INDEX IF NOT EXISTS schedules_next_occurrence
    ON schedules(next_occurrence)
"""


def _from_row(row: tuple[object, ...]) -> Schedule:
    rule: ScheduleRule = rule_from_json(json.loads(str(row[2])))
    return Schedule(
        id=str(row[0]),
        content=str(row[1]),
        rule=rule,
        next_occurrence=int(row[3]),  # type: ignore[arg-type]
        created_at=int(row[4]),  # type: ignore[arg-type]
        updated_at=int(row[5]),  # type: ignore[arg-type]
    )


def _zone(value: str | ZoneInfo) -> ZoneInfo:
    return value if isinstance(value, ZoneInfo) else ZoneInfo(value)


class SqliteScheduleStore:
    """Implémentation SQLite du port ``ScheduleStore``.

    La connexion est paresseuse : construire le store ne touche pas le disque,
    ce qui laisse une instance en Installation démarrer sans créer
    ``schedule.db``. ``create``, ``update`` et ``delete`` sont les seules
    écritures, chacune sous le verrou et suivie d'un ``commit``.
    """

    def __init__(
        self,
        *,
        db_path: str,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._db_path = db_path
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def create(self, schedule: Schedule) -> Schedule:
        rule_json = json.dumps(
            rule_to_json(schedule.rule), ensure_ascii=False, separators=(",", ":")
        )
        with self._lock:
            db = self._get_db()
            row = db.execute("SELECT COUNT(*) FROM schedules").fetchone()
            if row is not None and int(row[0]) >= MAX_SCHEDULES:
                raise ScheduleError(
                    "too_many_schedules",
                    f"at most {MAX_SCHEDULES} schedules are allowed",
                )
            db.execute(
                "INSERT INTO schedules "
                "(id, content, rule_json, next_occurrence, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    schedule.id,
                    schedule.content,
                    rule_json,
                    schedule.next_occurrence,
                    schedule.created_at,
                    schedule.updated_at,
                ),
            )
            db.commit()
        emit(
            self._trace_sink,
            TRACE_SOURCE,
            "ScheduleCreated",
            payload={"id": schedule.id, "kind": schedule.rule.kind},
        )
        return schedule

    def list(self) -> Sequence[Schedule]:
        with self._lock:
            rows = self._get_db().execute(
                "SELECT id, content, rule_json, next_occurrence, created_at, "
                "updated_at FROM schedules "
                "ORDER BY next_occurrence ASC, id ASC"
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def get(self, schedule_id: str) -> Schedule | None:
        with self._lock:
            row = self._get_db().execute(
                "SELECT id, content, rule_json, next_occurrence, created_at, "
                "updated_at FROM schedules WHERE id = ?",
                (schedule_id,),
            ).fetchone()
        return None if row is None else _from_row(row)

    def count(self) -> int:
        with self._lock:
            row = self._get_db().execute("SELECT COUNT(*) FROM schedules").fetchone()
        return 0 if row is None else int(row[0])

    def claim_due(
        self,
        now_epoch: int,
        *,
        startup: bool = False,
        timezone: str | ZoneInfo | None = None,
    ) -> Sequence[Schedule]:
        """Réclame les Planifications dues, en **une** transaction.

        Les lignes dont ``next_occurrence <= now_epoch`` sont relues dans l'ordre
        de leur échéance, puis :

        - une **ponctuelle** est effacée et rendue telle qu'elle était à
          l'échéance (le Planificateur émet son contenu réellement dû) ; un
          démarrage la rattrape aussi, une fois, même très en retard ;
        - une **récurrente** est avancée au prochain créneau valide
          strictement postérieur à ``now_epoch`` (règles de ``schedule_rules``)
          et rendue — sauf à ``startup=True``, où elle n'est pas rendue : un
          redémarrage ne rejoue pas les créneaux manqués (ADR-0028), le
          Planificateur trace le saut. Une récurrence par intervalle garde sa
          phase : son pas part de l'échéance précédente, jamais de ``now``.

        ``timezone`` est le fuseau de l'instance, indispensable aux règles
        quotidiennes et hebdomadaires ; ``None`` retombe sur UTC, ce qui suffit à
        une lecture de test et ne concerne jamais une occurrence récurrente
        locale en production (le Planificateur injecte toujours le Réglage).
        Tout échec annule la transaction entière : aucune moitié de lot n'est
        écrite.
        """
        zone = ZoneInfo("UTC") if timezone is None else _zone(timezone)
        moment = datetime.fromtimestamp(now_epoch, tz=zone)
        claimed: list[Schedule] = []
        with self._lock:
            db = self._get_db()
            rows = db.execute(
                "SELECT id, content, rule_json, next_occurrence, created_at, "
                "updated_at FROM schedules WHERE next_occurrence <= ? "
                "ORDER BY next_occurrence ASC, id ASC",
                (now_epoch,),
            ).fetchall()
            due = [_from_row(row) for row in rows]
            try:
                for schedule in due:
                    if schedule.rule.kind == "once":
                        db.execute(
                            "DELETE FROM schedules WHERE id = ?", (schedule.id,)
                        )
                        claimed.append(schedule)
                        continue
                    advanced = advance_occurrence(
                        schedule.rule,
                        previous=schedule.next_occurrence,
                        now=moment,
                        timezone=zone,
                    )
                    db.execute(
                        "UPDATE schedules SET next_occurrence = ? WHERE id = ?",
                        (advanced, schedule.id),
                    )
                    if not startup:
                        claimed.append(schedule)
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return tuple(claimed)

    def update(self, schedule: Schedule) -> bool:
        """Remplace une Planification ; ``False`` si la ligne a disparu."""
        rule_json = json.dumps(
            rule_to_json(schedule.rule), ensure_ascii=False, separators=(",", ":")
        )
        with self._lock:
            db = self._get_db()
            cursor = db.execute(
                "UPDATE schedules SET content = ?, rule_json = ?, "
                "next_occurrence = ?, updated_at = ? WHERE id = ?",
                (
                    schedule.content,
                    rule_json,
                    schedule.next_occurrence,
                    schedule.updated_at,
                    schedule.id,
                ),
            )
            changed = cursor.rowcount > 0
            db.commit()
        if not changed:
            return False
        emit(
            self._trace_sink,
            TRACE_SOURCE,
            "ScheduleUpdated",
            payload={"id": schedule.id, "kind": schedule.rule.kind},
        )
        return True

    def delete(self, schedule_id: str) -> bool:
        """Efface une Planification ; ``False`` si elle n'existait pas."""
        with self._lock:
            db = self._get_db()
            cursor = db.execute(
                "DELETE FROM schedules WHERE id = ?", (schedule_id,)
            )
            deleted = cursor.rowcount > 0
            db.commit()
        if not deleted:
            return False
        emit(
            self._trace_sink,
            TRACE_SOURCE,
            "ScheduleDeleted",
            payload={"id": schedule_id},
        )
        return True

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            db = sqlite3.connect(self._db_path, check_same_thread=False)
            db.execute(_SCHEDULES_SCHEMA)
            db.execute(_SCHEDULES_INDEX)
            db.commit()
            self._db = db
        return self._db
