"""Règles de Planification : validation pure et prochaine occurrence (ADR-0028).

Le noyau reçoit un objet ``when`` rempli par le modèle, jamais une expression
cron. Ce module le traduit en ``ScheduleRule`` et calcule l'instant suivant en
UTC (epoch secondes), à la seconde près. Il ne lit ni l'horloge ni le disque :
``now`` et le fuseau sont injectés, ce qui rend les changements d'heure
testables sans temps réel.

Cinq formes de ``when`` produisent une règle :

- ``{"kind": "once", "delay_seconds": N}`` : N secondes après ``now`` ;
- ``{"kind": "once", "at": "YYYY-MM-DDTHH:MM:SS"}`` : instant local absolu ;
- ``{"kind": "interval", "every_seconds": N}`` : toutes les N secondes ;
- ``{"kind": "daily", "time": "HH:MM"}`` : chaque jour à l'heure locale ;
- ``{"kind": "weekly", "days": [1..7], "time": "HH:MM"}`` : jours ISO.

Une règle stockée est toujours canonique : la forme relative d'une ponctuelle
est figée en ``at`` (epoch UTC) au moment de la création.

Changement d'heure (heure locale de l'instance) : un créneau local inexistant
est sauté au prochain créneau valide de la même règle ; un créneau local doublé
n'est calculé qu'une fois (première occurrence, ``fold=0``). Une ponctuelle
absolue sur une heure inexistante est normalisée au prochain instant valide
(02:30 devient 03:30), jamais refusée.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Mapping
from zoneinfo import ZoneInfo

from server.core.ports import ScheduleError, ScheduleRule

#: Plafond du nombre de Planifications (constante du noyau, pas un Réglage).
MAX_SCHEDULES = 200
#: Période minimale d'une récurrence par intervalle.
MIN_INTERVAL_SECONDS = 60
#: Longueur maximale du contenu d'une Planification.
MAX_CONTENT_CHARS = 4096

_KINDS = frozenset({"once", "interval", "daily", "weekly"})
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def _invalid(message: str) -> ScheduleError:
    return ScheduleError("invalid_when", message)


def as_zone(value: str | ZoneInfo) -> ZoneInfo:
    """Fuseau d'instance, déjà résolu ou nom IANA : **une** seule conversion."""
    return value if isinstance(value, ZoneInfo) else ZoneInfo(value)


def _aware(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now


def _as_int(value: object) -> int | None:
    """Entier strict : ``bool`` et chaînes sont refusés (le modèle se trompe)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_time(value: object) -> tuple[int, int]:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise _invalid("when.time must be a local time formatted as HH:MM")
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _local_slot(
    day: date, hour: int, minute: int, second: int, tz: ZoneInfo
) -> int | None:
    """Instant UTC du créneau local, ou ``None`` s'il n'existe pas (DST)."""
    naive = datetime.combine(day, time(hour, minute, second))
    candidate = naive.replace(tzinfo=tz, fold=0)
    resolved = candidate.astimezone(timezone.utc).astimezone(tz)
    # Un créneau inexistant revient sur un autre mur local (02:30 -> 03:30).
    if resolved.replace(tzinfo=None) != naive:
        return None
    return int(candidate.timestamp())


def _local_absolute(value: object, tz: ZoneInfo) -> int:
    """Instant UTC d'un ``at`` local, seconde comprise.

    Un instant local **inexistant** (passage à l'heure d'été) n'est pas refusé :
    il est normalisé au prochain créneau valide par le même passage UTC que les
    récurrences (02:30 -> 03:30), comme l'exige l'ADR-0028.
    """
    if not isinstance(value, str) or not _AT_RE.match(value):
        raise _invalid(
            "when.at must be a local instant formatted as YYYY-MM-DDTHH:MM:SS"
        )
    try:
        naive = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise _invalid("when.at is not a valid calendar instant") from None
    slot = _local_slot(naive.date(), naive.hour, naive.minute, naive.second, tz)
    if slot is not None:
        return slot
    # Heure locale inexistante : le passage par UTC rend l'instant réel qui
    # suit le saut (02:30 CET devient 03:30 CEST).
    return int(naive.replace(tzinfo=tz, fold=0).timestamp())


def _epoch(value: int) -> int:
    """Refuse un instant hors de la plage représentable (saisie démesurée)."""
    try:
        datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise _invalid("when is too far in the future") from None
    return value


def _seconds(now: datetime) -> int:
    return int(now.timestamp())


def parse_when(
    when: object, *, now: datetime, timezone: ZoneInfo
) -> ScheduleRule:
    """Valide un ``when`` de Tool et rend la règle canonique.

    Lève ``ScheduleError`` avec un code stable ; ne lève jamais une erreur
    brute pour une saisie du modèle.
    """
    moment = _aware(now)
    if not isinstance(when, Mapping):
        raise _invalid("when must be an object")
    kind = when.get("kind")
    if not isinstance(kind, str) or kind not in _KINDS:
        raise _invalid(
            "when.kind must be one of: once, interval, daily, weekly"
        )

    if kind == "once":
        delay = when.get("delay_seconds")
        absolute = when.get("at")
        if (delay is None) == (absolute is None):
            raise _invalid(
                "when.kind 'once' needs exactly one of delay_seconds or at"
            )
        if delay is not None:
            seconds = _as_int(delay)
            if seconds is None:
                raise _invalid("when.delay_seconds must be an integer")
            if seconds <= 0:
                raise ScheduleError(
                    "not_future", "when must be strictly in the future"
                )
            at = _seconds(moment) + seconds
        else:
            at = _local_absolute(absolute, timezone)
        if at <= _seconds(moment):
            raise ScheduleError(
                "not_future", "when must be strictly in the future"
            )
        return ScheduleRule(kind="once", at=_epoch(at))

    if kind == "interval":
        seconds = _as_int(when.get("every_seconds"))
        if seconds is None:
            raise _invalid("when.every_seconds must be an integer")
        if seconds < MIN_INTERVAL_SECONDS:
            raise ScheduleError(
                "interval_too_short",
                f"when.every_seconds must be at least {MIN_INTERVAL_SECONDS}",
            )
        _epoch(_seconds(moment) + seconds)
        return ScheduleRule(kind="interval", every_seconds=seconds)

    hour, minute = _as_time(when.get("time"))
    if kind == "daily":
        return ScheduleRule(kind="daily", time_of_day=f"{hour:02d}:{minute:02d}")

    raw_days = when.get("days")
    if not isinstance(raw_days, (list, tuple)):
        raise _invalid("when.days must be a list of days from 1 to 7")
    if not raw_days:
        raise ScheduleError(
            "empty_days", "when.days must list at least one day (1=Monday..7=Sunday)"
        )
    days: set[int] = set()
    for raw_day in raw_days:
        day = _as_int(raw_day)
        if day is None or day < 1 or day > 7:
            raise _invalid("when.days must contain integers from 1 to 7")
        days.add(day)
    return ScheduleRule(
        kind="weekly",
        time_of_day=f"{hour:02d}:{minute:02d}",
        days=tuple(sorted(days)),
    )


def next_occurrence(
    rule: ScheduleRule, *, now: datetime, timezone: ZoneInfo
) -> int:
    """Prochain instant UTC de la règle, strictement après ``now``.

    Une ponctuelle rend son instant figé (la validation de création a garanti
    qu'il était futur) ; un intervalle repart d'un pas depuis ``now``.
    """
    moment = _aware(now)
    if rule.kind == "once":
        if rule.at is None:
            raise ValueError("once rule without at")
        return _epoch(rule.at)
    if rule.kind == "interval":
        if rule.every_seconds is None:
            raise ValueError("interval rule without every_seconds")
        return _epoch(_seconds(moment) + rule.every_seconds)
    if rule.kind not in ("daily", "weekly"):
        raise ValueError(f"unknown schedule rule kind: {rule.kind!r}")
    if rule.time_of_day is None:
        raise ValueError(f"{rule.kind} rule without time_of_day")

    local = moment.astimezone(timezone)
    hour, minute = _as_time(rule.time_of_day)
    now_ts = moment.timestamp()
    selected = set(rule.days) if rule.kind == "weekly" else set(range(1, 8))
    # 15 jours couvrent un jour unique sauté par un changement d'heure.
    for offset in range(15):
        day = local.date() + timedelta(days=offset)
        if day.isoweekday() not in selected:
            continue
        slot = _local_slot(day, hour, minute, 0, timezone)
        if slot is not None and slot > now_ts:
            return slot
    raise ScheduleError("invalid_when", "no valid occurrence could be computed")


def advance_occurrence(
    rule: ScheduleRule, *, previous: int, now: datetime, timezone: ZoneInfo
) -> int:
    """Prochaine occurrence d'une récurrente après un créneau dû.

    Elle est toujours **strictement postérieure** à ``now`` et ne rejoue jamais
    un créneau passé. L'intervalle garde sa **phase** : le pas part de
    ``previous``, jamais de ``now``, donc une boucle en retard de quelques
    secondes ne dérive pas la récurrence et plusieurs périodes ratées sont
    sautées d'un coup (``previous + n * every_seconds``). Les règles
    quotidiennes et hebdomadaires reprennent au prochain créneau local valide.
    """
    moment = _aware(now)
    if rule.kind == "interval":
        every = rule.every_seconds
        if every is None or every <= 0:
            raise ValueError("interval rule without every_seconds")
        now_epoch = _seconds(moment)
        # Plus petit n >= 1 tel que previous + n * every > now.
        steps = max(1, (now_epoch - previous) // every + 1)
        return _epoch(previous + steps * every)
    return next_occurrence(rule, now=moment, timezone=timezone)


def rule_to_json(rule: ScheduleRule) -> dict[str, object]:
    """Forme canonique persistée : la ponctuelle porte son instant UTC."""
    if rule.kind == "once":
        if rule.at is None:
            raise ValueError("once rule without at")
        return {"kind": "once", "at": rule.at}
    if rule.kind == "interval":
        if rule.every_seconds is None:
            raise ValueError("interval rule without every_seconds")
        return {"kind": "interval", "every_seconds": rule.every_seconds}
    if rule.kind == "daily":
        return {"kind": "daily", "time": rule.time_of_day}
    if rule.kind == "weekly":
        return {"kind": "weekly", "days": list(rule.days), "time": rule.time_of_day}
    raise ValueError(f"unknown schedule rule kind: {rule.kind!r}")


def rule_from_json(payload: object) -> ScheduleRule:
    """Relit une règle canonique ; une ligne corrompue est une erreur de noyau."""
    if not isinstance(payload, Mapping):
        raise _invalid("stored rule is not an object")
    kind = payload.get("kind")
    if kind == "once":
        at = _as_int(payload.get("at"))
        if at is None:
            raise _invalid("stored once rule without at")
        return ScheduleRule(kind="once", at=at)
    if kind == "interval":
        seconds = _as_int(payload.get("every_seconds"))
        if seconds is None:
            raise _invalid("stored interval rule without every_seconds")
        return ScheduleRule(kind="interval", every_seconds=seconds)
    if kind == "daily":
        hour, minute = _as_time(payload.get("time"))
        return ScheduleRule(kind="daily", time_of_day=f"{hour:02d}:{minute:02d}")
    if kind == "weekly":
        hour, minute = _as_time(payload.get("time"))
        raw_days = payload.get("days")
        if not isinstance(raw_days, (list, tuple)) or not raw_days:
            raise _invalid("stored weekly rule without days")
        days: set[int] = set()
        for raw_day in raw_days:
            day = _as_int(raw_day)
            if day is None or not 1 <= day <= 7:
                raise _invalid("stored weekly rule has invalid days")
            days.add(day)
        return ScheduleRule(
            kind="weekly", time_of_day=f"{hour:02d}:{minute:02d}", days=tuple(sorted(days))
        )
    raise _invalid("stored rule has an unknown kind")
