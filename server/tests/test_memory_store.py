from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from server.adapters.memory import SqliteMemoryStore
from server.core import Coordinator, OpenLiveConnection, SessionRequested

_DIM = 384


class FakeEmbedder:
    """Deterministic embedder; keeps the suite free of torch/sentence-transformers."""

    def __init__(self, dim: int = _DIM) -> None:
        self.dim = dim
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.01] * self.dim


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


@pytest.fixture
def store(tmp_path) -> SqliteMemoryStore:
    return SqliteMemoryStore(
        db_path=str(tmp_path / "memory.db"),
        embedder=FakeEmbedder(),
        short_term_window=3,
        journal_retention_days=3,
    )


def test_append_then_read_back_the_recent_window_in_order(store) -> None:
    async def scenario() -> None:
        await store.append_turn("alice", "user", "bonjour")
        await store.append_turn("alice", "assistant", "salut")
        context = await store.session_context("alice")

        assert "user: bonjour" in context
        assert "assistant: salut" in context
        assert context.index("bonjour") < context.index("salut")

    asyncio.run(scenario())


def test_recent_window_is_limited_and_user_scoped(store) -> None:
    async def scenario() -> None:
        for i in range(5):
            await store.append_turn("alice", "user", f"alice-{i}")
            await store.append_turn("bob", "user", f"bob-{i}")

        alice = await store.session_context("alice")
        assert "alice-4" in alice and "alice-2" in alice
        assert "alice-1" not in alice and "alice-0" not in alice
        assert "bob" not in alice

        bob = await store.session_context("bob")
        assert "bob-4" in bob
        assert "alice" not in bob

    asyncio.run(scenario())


def test_empty_journal_yields_empty_context(store) -> None:
    async def scenario() -> None:
        assert await store.session_context("alice") == ""

    asyncio.run(scenario())


def test_profile_is_empty_for_now_but_the_seam_is_present(store) -> None:
    async def scenario() -> None:
        await store.append_turn("alice", "user", "salut")
        context = await store.session_context("alice")
        assert "faits épinglés" not in context

    asyncio.run(scenario())


def test_purge_removes_entries_older_than_retention(store) -> None:
    async def scenario() -> None:
        await store.append_turn("alice", "user", "vieux souvenir")
        old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        store._get_db().execute(
            "UPDATE journal SET created_at = ? WHERE user_id = ?", (old, "alice")
        )
        store._get_db().commit()

        deleted = await store.purge_older_than()
        assert deleted == 1
        assert await store.session_context("alice") == ""

    asyncio.run(scenario())


def test_append_purges_expired_entries_on_the_way_in(store) -> None:
    async def scenario() -> None:
        await store.append_turn("alice", "user", "ancien")
        old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        store._get_db().execute(
            "UPDATE journal SET created_at = ? WHERE user_id = ?", (old, "alice")
        )
        store._get_db().commit()

        await store.append_turn("alice", "user", "récent")
        context = await store.session_context("alice")
        assert "récent" in context
        assert "ancien" not in context

    asyncio.run(scenario())


def test_database_is_created_lazily(tmp_path) -> None:
    db_path = tmp_path / "lazy.db"
    SqliteMemoryStore(db_path=str(db_path), embedder=FakeEmbedder())
    assert not db_path.exists()


def test_recent_turns_reach_the_next_session_instruction(store) -> None:
    async def scenario() -> None:
        await store.append_turn("alice", "user", "ma couleur préférée est le bleu")
        coordinator = Coordinator(
            memory=store, user_id="alice", system_prompt="SYSTÈME"
        )

        commands = await coordinator.handle(SessionRequested(client_id="c1"))

        opened = next(c for c in commands if isinstance(c, OpenLiveConnection))
        assert opened.instruction.startswith("SYSTÈME")
        assert "ma couleur préférée est le bleu" in opened.instruction

    asyncio.run(scenario())


def _store(tmp_path, embedder) -> SqliteMemoryStore:
    return SqliteMemoryStore(db_path=str(tmp_path / "memory.db"), embedder=embedder)


def test_recall_ranks_journal_entries_by_cosine_distance(tmp_path) -> None:
    store = _store(tmp_path, BagOfWordsEmbedder())

    async def scenario() -> None:
        await store.append_turn("alice", "user", "mon chien s'appelle Rex")
        await store.append_turn("alice", "user", "j'aime les voitures rouges")
        hits = await store.recall("alice", "chien")

        assert hits
        assert hits[0].text == "mon chien s'appelle Rex"
        assert hits[0].source == "journal"
        assert hits[0].distance <= 1.0

    asyncio.run(scenario())


def test_recall_is_user_scoped(tmp_path) -> None:
    store = _store(tmp_path, BagOfWordsEmbedder())

    async def scenario() -> None:
        await store.append_turn("bob", "user", "le chien de bob")
        assert await store.recall("alice", "chien") == []

    asyncio.run(scenario())


def test_recall_merges_long_term_facts(tmp_path) -> None:
    embedder = BagOfWordsEmbedder()
    store = _store(tmp_path, embedder)

    async def scenario() -> None:
        db = store._get_db()
        db.execute(
            "INSERT INTO facts (embedding, user_id, content, pinned, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (json.dumps(embedder.encode("fait durable à retenir")), "alice",
             "fait durable à retenir", 0),
        )
        db.commit()

        hits = await store.recall("alice", "fait durable")

        assert any(h.text == "fait durable à retenir" and h.source == "fact" for h in hits)

    asyncio.run(scenario())


def test_fact_sources_link_table_is_created(tmp_path) -> None:
    store = _store(tmp_path, BagOfWordsEmbedder())
    row = store._get_db().execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fact_sources'"
    ).fetchone()
    assert row is not None


def test_recall_on_an_empty_journal_is_empty(store) -> None:
    async def scenario() -> None:
        assert await store.recall("alice", "rien") == []

    asyncio.run(scenario())


def test_saved_pinned_fact_is_injected_as_profile(store) -> None:
    async def scenario() -> None:
        await store.save("alice", "aime le bleu", pinned=True)
        await store.save("alice", "a un chien", pinned=False)

        context = await store.session_context("alice")

        assert "faits épinglés" in context
        assert "aime le bleu" in context
        assert "a un chien" not in context

    asyncio.run(scenario())


def test_saved_facts_are_user_scoped(store) -> None:
    async def scenario() -> None:
        await store.save("alice", "aime le bleu", pinned=True)

        assert "aime le bleu" in await store.session_context("alice")
        assert await store.session_context("bob") == ""

    asyncio.run(scenario())


def test_saved_fact_is_recallable(tmp_path) -> None:
    store = _store(tmp_path, BagOfWordsEmbedder())

    async def scenario() -> None:
        await store.save("alice", "le chien s'appelle Rex", pinned=False)

        hits = await store.recall("alice", "chien")

        assert any(h.text == "le chien s'appelle Rex" and h.source == "fact" for h in hits)

    asyncio.run(scenario())


def test_forget_removes_the_nearest_fact_and_its_source_journal_turns(tmp_path) -> None:
    store = _store(tmp_path, BagOfWordsEmbedder())

    async def scenario() -> None:
        await store.append_turn("alice", "user", "je suis végétarien")
        await store.save("alice", "est végétarien", pinned=True)
        db = store._get_db()
        fact_id = db.execute(
            "SELECT rowid FROM facts WHERE user_id = 'alice'"
        ).fetchone()[0]
        journal_id = db.execute(
            "SELECT rowid FROM journal WHERE user_id = 'alice'"
        ).fetchone()[0]
        db.execute(
            "INSERT INTO fact_sources (fact_id, journal_id, user_id) VALUES (?, ?, ?)",
            (fact_id, journal_id, "alice"),
        )
        db.commit()

        removed = await store.forget("alice", "végétarien", 0.6)

        assert removed is True
        assert db.execute("SELECT COUNT(*) FROM facts WHERE user_id = 'alice'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM journal WHERE user_id = 'alice'").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM fact_sources WHERE user_id = 'alice'"
        ).fetchone()[0] == 0

    asyncio.run(scenario())


def test_forget_returns_false_when_nothing_is_close_enough(tmp_path) -> None:
    store = _store(tmp_path, BagOfWordsEmbedder())

    async def scenario() -> None:
        await store.save("alice", "le chien s'appelle Rex", pinned=False)

        removed = await store.forget("alice", "végétarien", 0.6)

        assert removed is False
        assert store._get_db().execute(
            "SELECT COUNT(*) FROM facts WHERE user_id = 'alice'"
        ).fetchone()[0] == 1

    asyncio.run(scenario())


def test_forget_is_user_scoped(tmp_path) -> None:
    store = _store(tmp_path, BagOfWordsEmbedder())

    async def scenario() -> None:
        await store.save("alice", "est végétarien", pinned=True)

        removed = await store.forget("bob", "végétarien", 0.6)

        assert removed is False
        assert "est végétarien" in await store.session_context("alice")

    asyncio.run(scenario())
