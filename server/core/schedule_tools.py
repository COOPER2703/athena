"""Tools du noyau de Planification (ADR-0028).

``create_schedule`` et ``list_schedules`` ouvrent la tranche #194 ;
``update_schedule`` et ``delete_schedule`` la complètent (#197).

Le modèle remplit un schéma structuré ; il ne compose jamais de cron. Les
erreurs de saisie et de domaine sont rendues comme un statut lisible, jamais
comme une exception : le contrat de ``ToolRegistry`` réserve la propagation aux
bugs du noyau. Le contenu d'une Planification reste une **donnée** figée : il
n'est ni exécuté ni interprété ici.

``on_change`` est la couture de réveil du Planificateur (#195) : elle est
appelée une fois après une création, une modification ou une suppression
réussie, jamais après une lecture, un ``not_found`` ou un refus. Sa valeur par
défaut ne fait rien.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from server.core.ports import (
    Schedule,
    ScheduleError,
    ScheduleRule,
    ScheduleStore,
    ToolSpec,
)
from server.core.schedule_rules import (
    MAX_CONTENT_CHARS,
    next_occurrence,
    parse_when,
    rule_to_json,
)
from server.core.tools import ToolRegistry

CREATE_SCHEDULE = "create_schedule"
LIST_SCHEDULES = "list_schedules"
UPDATE_SCHEDULE = "update_schedule"
DELETE_SCHEDULE = "delete_schedule"

_WHEN_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "description": (
        "Exactement une règle. once: delay_seconds (délai relatif) OU at "
        "(instant local YYYY-MM-DDTHH:MM:SS) ; interval: every_seconds "
        "(minimum 60) ; daily: time HH:MM local ; weekly: days [1..7] "
        "(1=lundi) et time HH:MM local."
    ),
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["once", "interval", "daily", "weekly"],
        },
        "delay_seconds": {
            "type": "integer",
            "description": "Délai relatif en secondes, strictement positif.",
        },
        "at": {
            "type": "string",
            "description": (
                "Instant local absolu, sans décalage : YYYY-MM-DDTHH:MM:SS."
            ),
        },
        "every_seconds": {
            "type": "integer",
            "description": "Période d'une récurrence par intervalle, ≥ 60.",
        },
        "time": {
            "type": "string",
            "description": "Heure locale au format HH:MM.",
        },
        "days": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1, "maximum": 7},
            "description": "Jours ISO : 1=lundi … 7=dimanche, sans doublon.",
        },
    },
    "required": ["kind"],
}

_CREATE_SPEC = ToolSpec(
    name=CREATE_SCHEDULE,
    description=(
        "Programme un rappel ou un rendez-vous récurrent. Le contenu est figé "
        "au moment de la création et sera rappelé à la prochaine occurrence. "
        "when porte exactement une règle (ponctuelle, intervalle, quotidienne "
        "ou hebdomadaire), interprétée dans le fuseau local de l'instance. "
        "Bornes : contenu ≤ 4096 caractères, intervalle ≥ 60 s, ponctuelle "
        "strictement future, au plus 200 Planifications."
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "Le rappel à prononcer plus tard, à la troisième personne "
                    "ou à l'infinitif, sans dépendre du contexte présent."
                ),
            },
            "when": _WHEN_SCHEMA,
        },
        "required": ["content", "when"],
    },
)

_LIST_SPEC = ToolSpec(
    name=LIST_SCHEDULES,
    description=(
        "Liste les Planifications enregistrées, de la plus proche à la plus "
        "lointaine, avec leur identifiant stable, leur contenu, leur règle et "
        "leur prochaine occurrence en heure locale."
    ),
    parameters={"type": "object", "properties": {}},
)


def _as_text(value: object) -> str:
    """Coerce une valeur de Tool en texte nettoyé, sans jamais lever."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _content(value: object) -> str:
    """Valide le contenu d'une Planification, avec un code stable."""
    text = _as_text(value)
    if not text:
        raise ScheduleError("empty_content", "content is required")
    if len(text) > MAX_CONTENT_CHARS:
        raise ScheduleError(
            "content_too_long",
            f"content must be at most {MAX_CONTENT_CHARS} characters",
        )
    return text


def _zone(value: str | ZoneInfo) -> ZoneInfo:
    return value if isinstance(value, ZoneInfo) else ZoneInfo(value)


def _local_iso(epoch: int, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(epoch, zone).isoformat(timespec="seconds")


def register_schedule_tools(
    registry: ToolRegistry,
    store: ScheduleStore,
    *,
    now: Callable[[], datetime],
    timezone: str | ZoneInfo,
    on_change: Callable[[], None] | None = None,
) -> ToolRegistry:
    """Enregistre les quatre Tools de Planification sur ``registry``.

    ``now`` rend un instant **aware** ; ``timezone`` fixe l'interprétation des
    heures locales. Une création, une modification ou une suppression réussie
    réveille le Planificateur par ``on_change`` (aucun effet par défaut).
    """
    zone = _zone(timezone)
    wake = on_change if on_change is not None else lambda: None

    async def create_schedule(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not isinstance(arguments, Mapping):
            return {"status": "error", "message": "arguments must be an object"}

        moment = now()
        try:
            content = _content(arguments.get("content"))
            rule: ScheduleRule = parse_when(
                arguments.get("when"), now=moment, timezone=zone
            )
            next_at = next_occurrence(rule, now=moment, timezone=zone)
            created_at = int(moment.timestamp())
            schedule = Schedule(
                id=f"sch_{uuid.uuid4().hex}",
                content=content,
                rule=rule,
                next_occurrence=next_at,
                created_at=created_at,
                updated_at=created_at,
            )
            store.create(schedule)
        except ScheduleError as error:
            return {"status": "error", "message": error.message}

        wake()
        return {
            "status": "ok",
            "id": schedule.id,
            "next_occurrence": _local_iso(schedule.next_occurrence, zone),
        }

    async def list_schedules(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        return {
            "status": "ok",
            "schedules": [
                {
                    "id": schedule.id,
                    "content": schedule.content,
                    "rule": rule_to_json(schedule.rule),
                    "next_occurrence": _local_iso(
                        schedule.next_occurrence, zone
                    ),
                }
                for schedule in store.list()
            ],
        }

    async def update_schedule(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not isinstance(arguments, Mapping):
            return {"status": "error", "message": "arguments must be an object"}

        schedule_id = _as_text(arguments.get("id"))
        if not schedule_id:
            return {"status": "error", "message": "id is required"}
        has_content = "content" in arguments
        has_when = "when" in arguments
        if not has_content and not has_when:
            return {
                "status": "error",
                "message": "at least one of content or when is required",
            }

        existing = store.get(schedule_id)
        if existing is None:
            return {"status": "not_found"}

        moment = now()
        try:
            content = (
                _content(arguments.get("content"))
                if has_content
                else existing.content
            )
            rule: ScheduleRule = (
                parse_when(arguments.get("when"), now=moment, timezone=zone)
                if has_when
                else existing.rule
            )
            replacement = Schedule(
                id=existing.id,
                content=content,
                rule=rule,
                next_occurrence=next_occurrence(rule, now=moment, timezone=zone),
                created_at=existing.created_at,
                updated_at=int(moment.timestamp()),
            )
            changed = store.update(replacement)
        except ScheduleError as error:
            return {"status": "error", "message": error.message}
        if not changed:
            return {"status": "not_found"}

        wake()
        return {
            "status": "updated",
            "id": replacement.id,
            "next_occurrence": _local_iso(replacement.next_occurrence, zone),
        }

    async def delete_schedule(
        arguments: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not isinstance(arguments, Mapping):
            return {"status": "error", "message": "arguments must be an object"}

        schedule_id = _as_text(arguments.get("id"))
        if not schedule_id:
            return {"status": "error", "message": "id is required"}
        if store.get(schedule_id) is None or not store.delete(schedule_id):
            return {"status": "not_found"}

        wake()
        return {"status": "deleted", "id": schedule_id}

    registry.register(_CREATE_SPEC, create_schedule)
    registry.register(_LIST_SPEC, list_schedules)
    registry.register(_UPDATE_SPEC, update_schedule)
    registry.register(_DELETE_SPEC, delete_schedule)
    return registry


_UPDATE_SPEC = ToolSpec(
    name=UPDATE_SCHEDULE,
    description=(
        "Change an existing schedule, identified by the stable id returned by "
        "create_schedule or list_schedules. Provide content, when, or both. "
        "The content and/or rule is replaced and the next occurrence is "
        "recomputed from now, so no slot of the previous rule is kept. The "
        "same bounds as create_schedule apply. An unknown id returns "
        "not_found instead of a confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Stable schedule id (sch_...).",
            },
            "content": {
                "type": "string",
                "description": (
                    "The replacement reminder, stated in the third person or "
                    "as an infinitive, independent of the current context."
                ),
            },
            "when": _WHEN_SCHEMA,
        },
        "required": ["id"],
    },
)

_DELETE_SPEC = ToolSpec(
    name=DELETE_SCHEDULE,
    description=(
        "Delete an existing schedule identified by its stable id. Returns "
        "deleted on success and not_found when no schedule carries that id; "
        "deleting twice is not a silent success."
    ),
    parameters={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Stable schedule id (sch_...).",
            }
        },
        "required": ["id"],
    },
)
