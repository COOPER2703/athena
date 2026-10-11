from __future__ import annotations

import asyncio
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
