from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from conftest import RecordingTraceSink, wait_until
from test_app_root import FakeAdapter, FakeClock
from server.app.root import App, build_app
from server.app.config import AppConfig, GeminiConfig
from server.core.commands import OpenLiveConnection
from server.core.consolidation import (
    Consolidation,
    ConsolidationPolicy,
    ConsolidationReport,
    ConsolidationScheduler,
    local_now,
)
from server.core.events import SessionRequested
from server.core.ports import JournalEntry, MemoryHit, TraceEntry

_TZ = timezone(timedelta(hours=2))


def _dt(day: int = 1, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, minute, tzinfo=_TZ)


class FakeStore:
    def __init__(
        self,
        *,
        entries: list[JournalEntry] | None = None,
        last: datetime | None = None,
    ) -> None:
        self.entries = list(entries or [])
        self.last = last
        self.facts: list[tuple[str, bool]] = []
        self.marks: list[datetime] = []
        self.purged: list[int | None] = []
        self.queries: list[str] = []

    async def session_context(self, user_id: str) -> str:
        return ""

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        pass

    async def recall(self, user_id: str, query: str) -> list[MemoryHit]:
        self.queries.append(query)
        return []

    async def save(self, user_id: str, content: str, *, pinned: bool) -> None:
        pass

    async def forget(self, user_id: str, query: str, max_distance: float) -> bool:
        return False

    async def journal_entries_since(
        self, user_id: str, since: datetime | None
    ) -> list[JournalEntry]:
        return list(self.entries)

    async def last_consolidation(self, user_id: str) -> datetime | None:
        return self.last

    async def mark_consolidated(self, user_id: str, when: datetime) -> None:
        self.marks.append(when)
        self.last = when

    async def is_duplicate(self, user_id: str, text: str, max_distance: float) -> bool:
        return False

    async def append_fact(self, user_id: str, text: str, *, pinned: bool = False) -> None:
        self.facts.append((text, pinned))

    async def purge_older_than(self, days: int | None = None) -> int:
        self.purged.append(days)
        return 0


class StaticExtractor:
    def __init__(self, facts: list[str]) -> None:
        self._facts = list(facts)

    async def extract(self, transcript: str) -> list[str]:
        return list(self._facts)


class BlockingExtractor:
    """Extractor that blocks until released; proves consolidation is off-path."""

    def __init__(self, facts: list[str]) -> None:
        self._facts = list(facts)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(self, transcript: str) -> list[str]:
        self.started.set()
        await self.release.wait()
        return list(self._facts)


class _StopScheduler(Exception):
    """Test-only signal raised from the injected sleep to end the loop."""


class ManualClock:
    def __init__(self, start: datetime, max_sleeps: int) -> None:
        self.now = start
        self.max_sleeps = max_sleeps
        self.delays: list[float] = []

    def now_fn(self) -> datetime:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.now += timedelta(seconds=delay)
        if len(self.delays) >= self.max_sleeps:
            raise _StopScheduler()


def _scheduler(
    store: FakeStore,
    extractor,
    clock: ManualClock,
    *,
    on_run=None,
) -> ConsolidationScheduler:
    consolidation = Consolidation(
        memory=store,
        extractor=extractor,
        policy=ConsolidationPolicy((0, 12)),
        user_id="alice",
    )
    return ConsolidationScheduler(
        consolidation=consolidation,
        policy=consolidation.policy,
        now=clock.now_fn,
        sleep=clock.sleep,
        on_run=on_run,
    )


def test_local_now_is_timezone_aware() -> None:
    assert local_now().tzinfo is not None


def test_scheduler_fires_at_the_fixed_slots() -> None:
    store = FakeStore(entries=[JournalEntry("user", "salut")], last=None)
    extractor = StaticExtractor(["un fait"])
    clock = ManualClock(_dt(hour=8), max_sleeps=3)
    scheduler = _scheduler(store, extractor, clock)

    async def scenario() -> None:
        with pytest.raises(_StopScheduler):
            await scheduler.run()

    asyncio.run(scenario())

    assert store.marks == [_dt(hour=8), _dt(hour=12), _dt(day=2, hour=0)]
    assert clock.delays == [4 * 3600, 12 * 3600, 12 * 3600]


def test_scheduler_catches_up_a_missed_slot() -> None:
    store = FakeStore(entries=[JournalEntry("user", "salut")], last=_dt(day=1, hour=0))
    extractor = StaticExtractor(["un fait"])
    clock = ManualClock(_dt(day=2, hour=15), max_sleeps=1)
    scheduler = _scheduler(store, extractor, clock)

    async def scenario() -> None:
        with pytest.raises(_StopScheduler):
            await scheduler.run()

    asyncio.run(scenario())

    assert store.marks == [_dt(day=2, hour=15)]
    assert store.facts == [("un fait", False)]


def test_scheduler_does_not_run_twice_for_the_same_slot() -> None:
    store = FakeStore(entries=[JournalEntry("user", "salut")], last=_dt(hour=12))
    extractor = StaticExtractor(["un fait"])
    clock = ManualClock(_dt(hour=12, minute=30), max_sleeps=1)
    scheduler = _scheduler(store, extractor, clock)

    async def scenario() -> None:
        with pytest.raises(_StopScheduler):
            await scheduler.run()

    asyncio.run(scenario())

    assert store.marks == []
    assert store.facts == []


def test_scheduler_reports_runs_to_the_trace_callback() -> None:
    store = FakeStore(entries=[JournalEntry("user", "salut")], last=None)
    clock = ManualClock(_dt(hour=8), max_sleeps=1)
    reports: list[ConsolidationReport] = []
    scheduler = _scheduler(store, StaticExtractor(["fait"]), clock, on_run=reports.append)

    async def scenario() -> None:
        with pytest.raises(_StopScheduler):
            await scheduler.run()

    asyncio.run(scenario())

    assert len(reports) == 1
    assert reports[0].ran is True
    assert reports[0].facts_stored == 1


def test_consolidation_does_not_block_an_in_progress_session() -> None:
    store = FakeStore(entries=[JournalEntry("user", "salut")], last=None)
    extractor = BlockingExtractor(["un fait long terme"])
    clock = ManualClock(_dt(hour=8), max_sleeps=1)
    scheduler = _scheduler(store, extractor, clock)

    transport = FakeAdapter()
    gemini = FakeAdapter()
    idle_clock = FakeClock()
    sink = RecordingTraceSink()
    app = App(
        transport=transport,
        gemini=gemini,
        clock=idle_clock,
        trace_sink=sink,
        memory=store,
        user_id="alice",
        scheduler=scheduler,
    )

    async def scenario() -> None:
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(lambda: transport.started)
            await wait_until(lambda: extractor.started.is_set())

            await transport.feed(SessionRequested(client_id="c1"))
            await wait_until(lambda: gemini.of_type(OpenLiveConnection))
            assert not extractor.release.is_set()

            extractor.release.set()
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    asyncio.run(scenario())


def test_run_leaves_no_orphan_task_with_a_scheduler() -> None:
    store = FakeStore(entries=[], last=_dt(hour=0))
    clock = ManualClock(_dt(hour=8), max_sleeps=1000)
    scheduler = _scheduler(store, StaticExtractor([]), clock)

    transport = FakeAdapter()
    gemini = FakeAdapter()
    idle_clock = FakeClock()
    sink = RecordingTraceSink()
    app = App(
        transport=transport,
        gemini=gemini,
        clock=idle_clock,
        trace_sink=sink,
        memory=store,
        user_id="alice",
        scheduler=scheduler,
    )

    async def scenario() -> None:
        runner = asyncio.create_task(app.run())
        await wait_until(lambda: transport.started)
        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)

        pending = [
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        ]
        assert pending == []

    asyncio.run(scenario())


def test_build_app_wires_a_consolidation_scheduler() -> None:
    app = build_app(AppConfig(gemini=GeminiConfig(api_key="test-key")))
    assert app.scheduler is not None


def test_consolidation_trace_enters_the_single_stream() -> None:
    sink = RecordingTraceSink()
    store = FakeStore(entries=[JournalEntry("user", "salut")], last=None)
    clock = ManualClock(_dt(hour=8), max_sleeps=1)

    def on_run(report: ConsolidationReport) -> None:
        sink.emit(
            TraceEntry(
                timestamp=0.0,
                source="consolidation",
                kind="Consolidation",
                payload={"facts_stored": report.facts_stored},
            )
        )

    scheduler = _scheduler(store, StaticExtractor(["fait"]), clock, on_run=on_run)

    async def scenario() -> None:
        with pytest.raises(_StopScheduler):
            await scheduler.run()

    asyncio.run(scenario())

    assert sink.of_kind("Consolidation")
