from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

from server.adapters.memory import SqliteMemoryStore
from server.core.consolidation import Consolidation, ConsolidationPolicy

_DIM = 384


class BagOfWordsEmbedder:
    """Deterministic bag-of-words embedding so cosine ranking is meaningful."""

    def __init__(self, dim: int = _DIM) -> None:
        self.dim = dim

    def encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for token in text.lower().split():
            index = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim
            vector[index] += 1.0
        return vector

    async def embed(self, text: str) -> list[float]:
        return self.encode(text)


class StaticExtractor:
    def __init__(self, facts: list[str]) -> None:
        self._facts = list(facts)

    async def extract(self, transcript: str) -> list[str]:
        return list(self._facts)


def _store(tmp_path) -> SqliteMemoryStore:
    return SqliteMemoryStore(
        db_path=str(tmp_path / "memory.db"),
        embedder=BagOfWordsEmbedder(),
        journal_retention_days=3,
    )


def test_append_fact_then_recall_finds_a_durable_fact(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        await store.append_fact("alice", "L'utilisateur habite à Lyon")
        hits = await store.recall("alice", "habite Lyon")

        assert any(
            hit.text == "L'utilisateur habite à Lyon" and hit.source == "fact"
            for hit in hits
        )

    asyncio.run(scenario())


def test_append_fact_stores_the_pinned_flag(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        await store.append_fact("alice", "profil", pinned=True)
        await store.append_fact("alice", "simple fait", pinned=False)
        db = store._get_db()

        pinned = db.execute(
            "SELECT content, pinned FROM facts WHERE user_id = ?", ("alice",)
        ).fetchall()

        assert ("profil", 1) in pinned
        assert ("simple fait", 0) in pinned

    asyncio.run(scenario())


def test_is_duplicate_compares_against_the_nearest_fact(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        await store.append_fact("alice", "L'utilisateur aime le café")

        assert await store.is_duplicate("alice", "L'utilisateur aime le café", 0.15)
        assert not await store.is_duplicate(
            "alice", "Une phrase totalement sans rapport", 0.15
        )

    asyncio.run(scenario())


def test_is_duplicate_is_false_without_any_fact(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        assert not await store.is_duplicate("alice", "n'importe quoi", 0.15)

    asyncio.run(scenario())


def test_is_duplicate_is_user_scoped(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        await store.append_fact("bob", "L'utilisateur aime le café")
        assert not await store.is_duplicate("alice", "L'utilisateur aime le café", 0.15)

    asyncio.run(scenario())


def test_last_consolidation_is_none_before_the_first_run(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        assert await store.last_consolidation("alice") is None

    asyncio.run(scenario())


def test_mark_consolidated_round_trips_the_instant(tmp_path) -> None:
    store = _store(tmp_path)
    when = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)

    async def scenario() -> None:
        await store.mark_consolidated("alice", when)
        assert await store.last_consolidation("alice") == when

        later = when + timedelta(hours=12)
        await store.mark_consolidated("alice", later)
        assert await store.last_consolidation("alice") == later

    asyncio.run(scenario())


def test_journal_entries_since_returns_only_the_window_in_order(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        await store.append_turn("alice", "user", "vieux")
        await store.append_turn("alice", "assistant", "récent")

        db = store._get_db()
        old = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        db.execute(
            "UPDATE journal SET created_at = ? WHERE content = ?", (old, "vieux")
        )
        db.commit()

        since = datetime.now(timezone.utc) - timedelta(hours=1)
        entries = await store.journal_entries_since("alice", since)

        assert [(entry.role, entry.text) for entry in entries] == [
            ("assistant", "récent")
        ]
        assert entries[0].id > 0

    asyncio.run(scenario())


def test_journal_entries_since_none_returns_the_whole_journal(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        await store.append_turn("alice", "user", "un")
        await store.append_turn("alice", "user", "deux")

        entries = await store.journal_entries_since("alice", None)

        assert [entry.text for entry in entries] == ["un", "deux"]

    asyncio.run(scenario())


def test_forget_cascades_to_the_source_journal_turns(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        await store.append_turn(
            "alice", "user", "je travaille comme développeur Python"
        )
        await store.append_turn("alice", "assistant", "noté")
        consolidation = Consolidation(
            memory=store,
            extractor=StaticExtractor(["L'utilisateur est développeur Python"]),
            policy=ConsolidationPolicy((0, 12)),
            user_id="alice",
        )

        await consolidation.run(datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc))

        db = store._get_db()
        stored_sources = db.execute(
            "SELECT COUNT(*) FROM fact_sources WHERE user_id = 'alice'"
        ).fetchone()[0]
        assert stored_sources > 0

        removed = await store.forget("alice", "développeur Python", 0.6)

        assert removed is True
        assert (
            db.execute(
                "SELECT COUNT(*) FROM journal WHERE user_id = 'alice'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT COUNT(*) FROM fact_sources WHERE user_id = 'alice'"
            ).fetchone()[0]
            == 0
        )

    asyncio.run(scenario())


def test_consolidation_makes_durable_facts_recallable(tmp_path) -> None:
    store = _store(tmp_path)

    async def scenario() -> None:
        await store.append_turn("alice", "user", "je travaille comme développeur Python")
        consolidation = Consolidation(
            memory=store,
            extractor=StaticExtractor(["L'utilisateur est développeur Python"]),
            policy=ConsolidationPolicy((0, 12)),
            user_id="alice",
        )

        report = await consolidation.run(datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc))
        hits = await store.recall("alice", "développeur Python")

        assert report.ran is True
        assert any(hit.source == "fact" for hit in hits)
        assert any(
            hit.text == "L'utilisateur est développeur Python" for hit in hits
        )

    asyncio.run(scenario())
