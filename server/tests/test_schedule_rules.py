"""Calcul pur des règles de Planification (couture basse, ADR-0028).

Ces tests n'observent que des instants : la règle structurée se valide et la
prochaine occurrence se calcule dans le fuseau de l'instance, puis se rend en
UTC à la seconde. Les valeurs attendues viennent d'exemples travaillés (heures
locales de Paris en hiver et en été), jamais d'un recalcul par le code testé.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from server.core.ports import ScheduleError, ScheduleRule
from server.core.schedule_rules import (
    MAX_CONTENT_CHARS,
    MAX_SCHEDULES,
    MIN_INTERVAL_SECONDS,
    next_occurrence,
    parse_when,
    rule_from_json,
    rule_to_json,
)

PARIS = ZoneInfo("Europe/Paris")


def _utc(*parts: int) -> int:
    return int(datetime(*parts, tzinfo=timezone.utc).timestamp())


def test_the_kernel_bounds_are_the_documented_ones() -> None:
    assert MAX_SCHEDULES == 200
    assert MIN_INTERVAL_SECONDS == 60
    assert MAX_CONTENT_CHARS == 4096


def test_an_interval_rule_fires_one_period_after_now() -> None:
    now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=PARIS)

    rule = parse_when(
        {"kind": "interval", "every_seconds": 600}, now=now, timezone=PARIS
    )

    assert rule == ScheduleRule(kind="interval", every_seconds=600)
    assert next_occurrence(rule, now=now, timezone=PARIS) == _utc(
        2026, 1, 1, 9, 10
    )


def test_an_interval_below_one_minute_is_refused() -> None:
    now = datetime(2026, 1, 1, 10, 0, 0, tzinfo=PARIS)

    with pytest.raises(ScheduleError) as excinfo:
        parse_when(
            {"kind": "interval", "every_seconds": 59}, now=now, timezone=PARIS
        )

    assert excinfo.value.code == "interval_too_short"


def test_a_daily_rule_takes_the_slot_ahead_and_rolls_over_to_tomorrow() -> None:
    rule = parse_when({"kind": "daily", "time": "08:00"}, now=datetime(
        2026, 1, 1, 7, 0, tzinfo=PARIS
    ), timezone=PARIS)

    assert rule == ScheduleRule(kind="daily", time_of_day="08:00")
    assert next_occurrence(
        rule, now=datetime(2026, 1, 1, 7, 0, tzinfo=PARIS), timezone=PARIS
    ) == _utc(2026, 1, 1, 7, 0)
    # 08:00 Paris en hiver = 07:00 UTC.
    assert next_occurrence(
        rule, now=datetime(2026, 1, 1, 9, 0, tzinfo=PARIS), timezone=PARIS
    ) == _utc(2026, 1, 2, 7, 0)


def test_a_weekly_rule_walks_to_the_next_selected_day() -> None:
    rule = parse_when(
        {"kind": "weekly", "days": [1, 3, 5], "time": "09:00"},
        now=datetime(2026, 1, 1, 10, 0, tzinfo=PARIS),
        timezone=PARIS,
    )

    assert rule == ScheduleRule(
        kind="weekly", time_of_day="09:00", days=(1, 3, 5)
    )
    # Jeudi 1er janvier 2026 : le prochain jour est vendredi 2.
    assert next_occurrence(
        rule, now=datetime(2026, 1, 1, 10, 0, tzinfo=PARIS), timezone=PARIS
    ) == _utc(2026, 1, 2, 8, 0)
    # Lundi 5 janvier à 07:00 : le créneau du jour est encore devant.
    assert next_occurrence(
        rule, now=datetime(2026, 1, 5, 7, 0, tzinfo=PARIS), timezone=PARIS
    ) == _utc(2026, 1, 5, 8, 0)


def test_an_empty_day_set_is_refused() -> None:
    with pytest.raises(ScheduleError) as excinfo:
        parse_when(
            {"kind": "weekly", "days": [], "time": "09:00"},
            now=datetime(2026, 1, 1, 10, 0, tzinfo=PARIS),
            timezone=PARIS,
        )

    assert excinfo.value.code == "empty_days"


def test_a_missing_local_time_is_skipped_to_the_next_valid_slot() -> None:
    """Passage à l'heure d'été : 02:30 n'existe pas le 29 mars 2026 à Paris."""
    rule = ScheduleRule(kind="daily", time_of_day="02:30")

    assert next_occurrence(
        rule, now=datetime(2026, 3, 28, 12, 0, tzinfo=PARIS), timezone=PARIS
    ) == _utc(2026, 3, 30, 0, 30)
    assert next_occurrence(
        rule, now=datetime(2026, 3, 29, 1, 0, tzinfo=PARIS), timezone=PARIS
    ) == _utc(2026, 3, 30, 0, 30)


def test_a_doubled_local_time_is_emitted_once() -> None:
    """Passage à l'heure d'hiver : 02:30 existe deux fois le 25 octobre 2026."""
    rule = ScheduleRule(kind="daily", time_of_day="02:30")

    # Depuis la veille, le créneau est la première occurrence (02:30 CEST).
    assert next_occurrence(
        rule, now=datetime(2026, 10, 24, 12, 0, tzinfo=PARIS), timezone=PARIS
    ) == _utc(2026, 10, 25, 0, 30)
    # Après la première occurrence (02:45 CEST), on ne la rejoue pas.
    assert next_occurrence(
        rule, now=datetime(2026, 10, 25, 2, 45, fold=0, tzinfo=PARIS),
        timezone=PARIS,
    ) == _utc(2026, 10, 26, 1, 30)
    # Pendant la seconde occurrence (02:15 CET), elle est déjà passée.
    assert next_occurrence(
        rule, now=datetime(2026, 10, 25, 2, 15, fold=1, tzinfo=PARIS),
        timezone=PARIS,
    ) == _utc(2026, 10, 26, 1, 30)


def test_a_once_rule_keeps_the_absolute_local_instant() -> None:
    rule = parse_when(
        {"kind": "once", "at": "2026-01-02T08:30:00"},
        now=datetime(2026, 1, 1, 10, 0, tzinfo=PARIS),
        timezone=PARIS,
    )

    assert rule == ScheduleRule(kind="once", at=_utc(2026, 1, 2, 7, 30))
    assert next_occurrence(
        rule, now=datetime(2026, 1, 1, 10, 0, tzinfo=PARIS), timezone=PARIS
    ) == _utc(2026, 1, 2, 7, 30)


def test_a_once_rule_honours_the_seconds_of_an_absolute_instant() -> None:
    rule = parse_when(
        {"kind": "once", "at": "2026-01-02T08:30:30"},
        now=datetime(2026, 1, 1, 10, 0, tzinfo=PARIS),
        timezone=PARIS,
    )

    # 08:30:30 Paris (hiver) = 07:30:30 UTC : la seconde n'est pas tronquée.
    assert rule.at == _utc(2026, 1, 2, 7, 30, 30)


def test_a_once_rule_a_few_seconds_ahead_in_the_same_minute_is_future() -> None:
    rule = parse_when(
        {"kind": "once", "at": "2026-01-01T10:00:30"},
        now=datetime(2026, 1, 1, 10, 0, 5, tzinfo=PARIS),
        timezone=PARIS,
    )

    assert rule.at == _utc(2026, 1, 1, 9, 0, 30)


def test_a_once_rule_in_a_spring_forward_gap_skips_to_the_valid_instant() -> None:
    """29 mars 2026 : 02:30 n'existe pas ; l'instant valide est 03:30 CEST."""
    rule = parse_when(
        {"kind": "once", "at": "2026-03-29T02:30:00"},
        now=datetime(2026, 3, 28, 12, 0, tzinfo=PARIS),
        timezone=PARIS,
    )

    assert rule.at == _utc(2026, 3, 29, 1, 30)


def test_a_once_rule_with_a_relative_delay_is_frozen_at_creation() -> None:
    now = datetime(2026, 1, 1, 10, 0, 0, 500000, tzinfo=PARIS)

    rule = parse_when({"kind": "once", "delay_seconds": 600}, now=now, timezone=PARIS)

    assert rule == ScheduleRule(kind="once", at=_utc(2026, 1, 1, 9, 10))


def test_a_once_rule_must_be_strictly_future() -> None:
    now = datetime(2026, 1, 1, 10, 0, tzinfo=PARIS)

    for when in (
        {"kind": "once", "delay_seconds": 0},
        {"kind": "once", "at": "2026-01-01T10:00:00"},
        {"kind": "once", "at": "2025-12-31T10:00:00"},
    ):
        with pytest.raises(ScheduleError) as excinfo:
            parse_when(when, now=now, timezone=PARIS)
        assert excinfo.value.code == "not_future"


def test_a_once_rule_accepts_the_first_occurrence_of_an_ambiguous_time() -> None:
    rule = parse_when(
        {"kind": "once", "at": "2026-10-25T02:30:00"},
        now=datetime(2026, 10, 24, 12, 0, tzinfo=PARIS),
        timezone=PARIS,
    )

    assert rule.at == _utc(2026, 10, 25, 0, 30)


@pytest.mark.parametrize(
    "when",
    [
        None,
        "demain",
        {},
        {"kind": "cron", "expression": "* * * * *"},
        {"kind": "daily", "time": "25:00"},
        {"kind": "daily", "time": "8h"},
        {"kind": "weekly", "days": [0, 1], "time": "09:00"},
        {"kind": "weekly", "days": [1, 8], "time": "09:00"},
        {"kind": "once", "at": "2026-01-02"},
        {"kind": "once", "at": "2026-01-02T08:30:00+01:00"},
        {"kind": "once", "at": "2026-02-30T08:30:00"},
        {"kind": "once", "delay_seconds": 10**20},
        {"kind": "interval", "every_seconds": "600"},
        {"kind": "interval", "every_seconds": 10**20},
        {"kind": "once"},
        {"kind": "once", "delay_seconds": 60, "at": "2026-01-02T08:30:00"},
    ],
)
def test_a_malformed_when_is_a_readable_invalid_when(when: object) -> None:
    with pytest.raises(ScheduleError) as excinfo:
        parse_when(
            when,
            now=datetime(2026, 1, 1, 10, 0, tzinfo=PARIS),
            timezone=PARIS,
        )

    assert excinfo.value.code == "invalid_when"
    assert excinfo.value.message


def test_rules_round_trip_through_their_json_shape() -> None:
    for rule in (
        ScheduleRule(kind="once", at=1_700_000_000),
        ScheduleRule(kind="interval", every_seconds=60),
        ScheduleRule(kind="daily", time_of_day="08:00"),
        ScheduleRule(kind="weekly", time_of_day="09:00", days=(1, 3, 5)),
    ):
        assert rule_from_json(rule_to_json(rule)) == rule
