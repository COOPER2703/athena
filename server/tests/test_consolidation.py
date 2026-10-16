from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from server.core.consolidation import (
    Consolidation,
    ConsolidationPolicy,
    ConsolidationReport,
)
from server.core.ports import JournalEntry, MemoryHit

_TZ = timezone(timedelta(hours=2))


def _dt(day: int = 1, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, minute, tzinfo=_TZ)


# --------------------------------------------------------------------------- #
# ConsolidationPolicy — pure fixed-slot scheduler with catch-up
# --------------------------------------------------------------------------- #


def test_next_slot_before_noon_is_noon() -> None:
    policy = ConsolidationPolicy((0, 12))
    assert policy.next_slot(_dt(hour=8)) == _dt(hour=12)


def test_next_slot_at_or_after_noon_is_midnight_next_day() -> None:
    policy = ConsolidationPolicy((0, 12))
    assert policy.next_slot(_dt(hour=12)) == _dt(day=2, hour=0)
    assert policy.next_slot(_dt(hour=23, minute=59)) == _dt(day=2, hour=0)


def test_delay_until_next_is_the_time_to_the_slot() -> None:
    policy = ConsolidationPolicy((0, 12))
    assert policy.delay_until_next(_dt(hour=8)) == 4 * 3600
    assert policy.delay_until_next(_dt(hour=12)) == 12 * 3600


def test_first_ever_run_is_due() -> None:
    policy = ConsolidationPolicy((0, 12))
    assert policy.due(_dt(hour=8), None) is True


def test_not_due_right_after_a_run_at_a_slot() -> None:
    policy = ConsolidationPolicy((0, 12))
    assert policy.due(_dt(hour=0, minute=5), _dt(hour=0)) is False
    assert policy.due(_dt(hour=11, minute=59), _dt(hour=0)) is False


def test_due_when_a_slot_was_crossed_since_the_last_run() -> None:
    policy = ConsolidationPolicy((0, 12))
    assert policy.due(_dt(hour=12), _dt(hour=0)) is True


def test_catch_up_when_a_slot_was_missed_while_offline() -> None:
    policy = ConsolidationPolicy((0, 12))
    assert policy.due(_dt(day=2, hour=15), _dt(day=1, hour=0)) is True


def test_never_due_twice_for_the_same_slot() -> None:
    policy = ConsolidationPolicy((0, 12))
    assert policy.due(_dt(hour=12), _dt(hour=12)) is False
    assert policy.due(_dt(hour=12, minute=1), _dt(hour=12)) is False
    assert policy.due(_dt(hour=23, minute=59), _dt(hour=12)) is False


def test_last_run_in_another_timezone_is_compared_by_instant() -> None:
    policy = ConsolidationPolicy((0, 12))
    last_utc = _dt(hour=12).astimezone(timezone.utc)
    assert policy.due(_dt(hour=13), last_utc) is False
    assert policy.due(_dt(day=2, hour=1), last_utc) is True


def test_policy_rejects_an_empty_schedule() -> None:
    import pytest

    with pytest.raises(ValueError):
        ConsolidationPolicy(())


# --------------------------------------------------------------------------- #
# Consolidation routine — pure policy over the MemoryStore port
# --------------------------------------------------------------------------- #


class FakeStore:
    def __init__(
        self,
        *,
        entries: list[JournalEntry] | None = None,
        duplicates: set[str] | None = None,
        last: datetime | None = None,
    ) -> None:
        self.entries = list(entries or [])
        self.duplicates = set(duplicates or [])
        self.last = last
        self.facts: list[tuple[str, bool]] = []
        self.sources: list[tuple[int, ...]] = []
        self.marks: list[datetime] = []
        self.purged: list[int | None] = []
        self.since_seen: list[datetime | None] = []
        self.duplicate_checks: list[tuple[str, float]] = []

    async def session_context(self, user_id: str) -> str:
        return ""

    async def pinned_facts(self, user_id: str) -> list[str]:
        return []

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        pass

    async def recall(self, user_id: str, query: str) -> list[MemoryHit]:
        return []

    async def save(self, user_id: str, content: str, *, pinned: bool) -> None:
        pass

    async def forget(self, user_id: str, query: str, max_distance: float) -> bool:
        return False

    async def journal_entries_since(
        self, user_id: str, since: datetime | None
    ) -> list[JournalEntry]:
        self.since_seen.append(since)
        return list(self.entries)

    async def last_consolidation(self, user_id: str) -> datetime | None:
        return self.last

    async def mark_consolidated(self, user_id: str, when: datetime) -> None:
        self.marks.append(when)
        self.last = when

    async def is_duplicate(self, user_id: str, text: str, max_distance: float) -> bool:
        self.duplicate_checks.append((text, max_distance))
        return text in self.duplicates

    async def append_fact(
        self,
        user_id: str,
        text: str,
        *,
        pinned: bool = False,
        source_ids: tuple[int, ...] = (),
    ) -> None:
        self.facts.append((text, pinned))
        self.sources.append(tuple(source_ids))

    async def purge_older_than(self, days: int | None = None) -> int:
        self.purged.append(days)
        return 0


class RecordingExtractor:
    def __init__(self, facts: list[str] | None = None) -> None:
        self._facts = list(facts or [])
        self.transcripts: list[str] = []

    async def extract(self, transcript: str) -> list[str]:
        self.transcripts.append(transcript)
        return list(self._facts)


def _consolidation(
    store: FakeStore,
    extractor: RecordingExtractor,
    *,
    policy: ConsolidationPolicy | None = None,
    **kwargs: object,
) -> Consolidation:
    return Consolidation(
        memory=store,
        extractor=extractor,
        policy=policy or ConsolidationPolicy((0, 12)),
        user_id="alice",
        **kwargs,
    )


def _run(consolidation: Consolidation, now: datetime) -> ConsolidationReport:
    return asyncio.run(consolidation.run(now))


def test_extracted_facts_are_stored_as_non_pinned_durable_facts() -> None:
    store = FakeStore(
        entries=[JournalEntry(1, "user", "j'habite à Lyon")],
        last=_dt(day=1, hour=0),
    )
    extractor = RecordingExtractor(["L'utilisateur habite à Lyon"])

    report = _run(_consolidation(store, extractor), _dt(hour=12))

    assert report.ran is True
    assert store.facts == [("L'utilisateur habite à Lyon", False)]
    assert report.facts_stored == 1


def test_event_turns_are_excluded_from_the_transcript() -> None:
    store = FakeStore(
        entries=[
            JournalEntry(1, "user", "bonjour"),
            JournalEntry(2, "event", "REUNION ANNULÉE"),
            JournalEntry(3, "assistant", "salut"),
        ],
        last=_dt(day=1, hour=0),
    )
    extractor = RecordingExtractor([])

    _run(_consolidation(store, extractor), _dt(hour=12))

    transcript = extractor.transcripts[0]
    assert "[user] bonjour" in transcript
    assert "[assistant] salut" in transcript
    assert "REUNION" not in transcript


def test_announced_event_fact_and_announcement_are_both_excluded() -> None:
    # Ticket #42 : le fait annoncé et la parole d'annonce sont journalisés en
    # rôle ``event`` ; ni l'un ni l'autre ne nourrit la Consolidation.
    store = FakeStore(
        entries=[
            JournalEntry(1, "event", "[matrix] porte ouverte"),
            JournalEntry(2, "event", "La porte d'entrée est ouverte"),
            JournalEntry(3, "user", "merci"),
        ],
        last=_dt(day=1, hour=0),
    )
    extractor = RecordingExtractor([])

    _run(_consolidation(store, extractor), _dt(hour=12))

    transcript = extractor.transcripts[0]
    assert "[matrix] porte ouverte" not in transcript
    assert "La porte d'entrée est ouverte" not in transcript
    assert transcript == "[user] merci"


def test_stored_facts_are_linked_to_the_non_event_source_journal_ids() -> None:
    store = FakeStore(
        entries=[
            JournalEntry(1, "user", "bonjour"),
            JournalEntry(2, "event", "REUNION ANNULÉE"),
            JournalEntry(3, "assistant", "salut"),
        ],
        last=_dt(day=1, hour=0),
    )

    _run(_consolidation(store, RecordingExtractor(["un fait"])), _dt(hour=12))

    assert store.facts == [("un fait", False)]
    assert store.sources == [(1, 3)]



def test_duplicates_are_skipped_using_the_dedup_threshold() -> None:
    store = FakeStore(
        entries=[JournalEntry(1, "user", "j'aime le café")],
        duplicates={"L'utilisateur aime le café"},
        last=_dt(day=1, hour=0),
    )
    extractor = RecordingExtractor(
        ["L'utilisateur aime le café", "L'utilisateur habite à Lyon"]
    )

    report = _run(_consolidation(store, extractor, dedup_threshold=0.15), _dt(hour=12))

    assert store.facts == [("L'utilisateur habite à Lyon", False)]
    assert report.duplicates == 1
    assert store.duplicate_checks == [
        ("L'utilisateur aime le café", 0.15),
        ("L'utilisateur habite à Lyon", 0.15),
    ]


def test_journal_is_purged_at_the_retention_setting() -> None:
    store = FakeStore(
        entries=[JournalEntry(1, "user", "salut")], last=_dt(day=1, hour=0)
    )

    _run(_consolidation(store, RecordingExtractor([]), journal_retention_days=3), _dt(hour=12))

    assert store.purged == [3]


def test_a_run_marks_the_consolidation_time() -> None:
    store = FakeStore(
        entries=[JournalEntry(1, "user", "salut")], last=_dt(day=1, hour=0)
    )
    now = _dt(hour=12)

    _run(_consolidation(store, RecordingExtractor([])), now)

    assert store.marks == [now]


def test_consolidation_never_pins() -> None:
    store = FakeStore(
        entries=[JournalEntry(1, "user", "je suis développeur")],
        last=_dt(day=1, hour=0),
    )
    extractor = RecordingExtractor(["L'utilisateur est développeur", "Il code en Python"])

    _run(_consolidation(store, extractor), _dt(hour=12))

    assert all(pinned is False for _, pinned in store.facts)


def test_an_undue_run_touches_nothing() -> None:
    store = FakeStore(
        entries=[JournalEntry(1, "user", "salut")], last=_dt(hour=0)
    )
    extractor = RecordingExtractor(["un fait"])

    report = _run(_consolidation(store, extractor), _dt(hour=8))

    assert report.ran is False
    assert store.facts == []
    assert store.purged == []
    assert store.marks == []
    assert extractor.transcripts == []


def test_an_event_only_window_skips_extraction_but_still_purges_and_marks() -> None:
    store = FakeStore(
        entries=[JournalEntry(1, "event", "quelque chose")],
        last=_dt(day=1, hour=0),
    )
    extractor = RecordingExtractor(["ne doit pas être appelé"])

    report = _run(_consolidation(store, extractor), _dt(hour=12))

    assert report.ran is True
    assert extractor.transcripts == []
    assert store.facts == []
    assert store.purged == [3]
    assert len(store.marks) == 1


def test_the_window_starts_at_the_last_consolidation() -> None:
    last = _dt(day=1, hour=0)
    store = FakeStore(entries=[JournalEntry(1, "user", "salut")], last=last)

    _run(_consolidation(store, RecordingExtractor([])), _dt(hour=12))

    assert store.since_seen == [last]
