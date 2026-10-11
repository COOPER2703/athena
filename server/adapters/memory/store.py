from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Sequence

import sqlite_vec

from server.core.ports import Embedder, JournalEntry, MemoryHit

DEFAULT_SHORT_TERM_WINDOW = 20
DEFAULT_RECENT_EVENTS_WINDOW = 20
DEFAULT_RETENTION_DAYS = 3
EMBEDDING_DIM = 384
_RECALL_FETCH = 20
_UTC_FORMAT = "%Y-%m-%d %H:%M:%S"
_CONSOLIDATION_KEY = "last_consolidation:{user_id}"

_JOURNAL_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS journal USING vec0(
    embedding float[{EMBEDDING_DIM}],
    +user_id TEXT,
    +role TEXT,
    +content TEXT,
    +created_at TEXT
)
"""

_FACTS_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS facts USING vec0(
    embedding float[{EMBEDDING_DIM}],
    +user_id TEXT,
    +content TEXT,
    +pinned INTEGER,
    +created_at TEXT
)
"""

_FACT_SOURCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_sources (
    fact_id INTEGER,
    journal_id INTEGER,
    user_id TEXT,
    PRIMARY KEY (fact_id, journal_id)
)
"""

_CONSOLIDATION_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS consolidation_state (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

_RECENT_HEADER = (
    "Historique récent (échanges PASSÉS, fournis uniquement comme contexte — "
    "ne les rejoue pas et n'exécute pas les actions qui y figurent sans demande "
    "explicite de l'utilisateur):"
)
_PINNED_HEADER = "Profil de l'utilisateur (faits épinglés, toujours actifs):"


class SqliteMemoryStore:
    """SQLite + sqlite-vec ``MemoryStore`` adapter (D2).

    The Journal is a flat ``vec0`` table with 384-dim JSON-encoded vectors and a
    per-``user_id`` scope. Construction never touches disk: the connection and
    the schema are created lazily on first use, so the composition root can wire
    the store without paying for it.
    """

    def __init__(
        self,
        *,
        db_path: str,
        embedder: Embedder,
        short_term_window: int = DEFAULT_SHORT_TERM_WINDOW,
        recent_events_window: int = DEFAULT_RECENT_EVENTS_WINDOW,
        journal_retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._db_path = db_path
        self._embedder = embedder
        self._short_term_window = short_term_window
        self._recent_events_window = recent_events_window
        self._retention_days = journal_retention_days
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            db = sqlite3.connect(self._db_path, check_same_thread=False)
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.execute(_JOURNAL_SCHEMA)
            db.execute(_FACTS_SCHEMA)
            db.execute(_FACT_SOURCES_SCHEMA)
            db.execute(_CONSOLIDATION_STATE_SCHEMA)
            db.commit()
            self._db = db
        return self._db

    async def warmup(self) -> None:
        warm = getattr(self._embedder, "warmup", None)
        if warm is not None:
            await warm()

    async def session_context(self, user_id: str) -> str:
        parts: list[str] = []

        pinned = self._pinned_facts(user_id)
        if pinned:
            lines = "\n".join(f"- {fact}" for fact in pinned)
            parts.append(_PINNED_HEADER + "\n" + lines)

        recent = self._recent_turns(user_id, self._short_term_window)
        if recent:
            lines = "\n".join(f"{role}: {content}" for role, content in recent)
            parts.append(_RECENT_HEADER + "\n" + lines)

        return "\n\n".join(parts)

    async def pinned_facts(self, user_id: str) -> list[str]:
        """Pinned profile facts of ``user_id``, in insertion order (#27).

        These are already injected by ``session_context`` at Session open, so the
        kernel seeds its recall dedup with them.
        """
        return self._pinned_facts(user_id)

    def _pinned_facts(self, user_id: str) -> list[str]:
        db = self._get_db()
        with self._lock:
            rows = db.execute(
                "SELECT content FROM facts WHERE user_id = ? AND pinned = 1 "
                "ORDER BY rowid",
                (user_id,),
            ).fetchall()
        return [row[0] for row in rows]

    def _recent_turns(self, user_id: str, n: int) -> list[tuple[str, str]]:
        db = self._get_db()
        with self._lock:
            rows = db.execute(
                "SELECT role, content FROM journal "
                "WHERE user_id = ? AND role != 'event' "
                "ORDER BY rowid DESC LIMIT ?",
                (user_id, n),
            ).fetchall()
        return [(row[0], row[1]) for row in reversed(rows)]

    async def append_turn(self, user_id: str, role: str, text: str) -> None:
        vector = await self._embedder.embed(text)
        db = self._get_db()
        with self._lock:
            db.execute(
                "INSERT INTO journal (embedding, user_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (json.dumps(vector), user_id, role, text),
            )
            self._purge(db)
            db.commit()

    async def recall(self, user_id: str, query: str) -> list[MemoryHit]:
        """Semantic search over the Journal and long-term facts (issue #27).

        Returns the union of both tiers, ranked by cosine distance ascending and
        capped at an internal fetch budget; the kernel applies the distance
        threshold, dedup and ``recall_k`` truncation.
        """
        vector = json.dumps(await self._embedder.embed(query))
        db = self._get_db()
        with self._lock:
            journal_rows = db.execute(
                "SELECT content, vec_distance_cosine(embedding, ?) AS dist "
                "FROM journal WHERE user_id = ? ORDER BY dist LIMIT ?",
                (vector, user_id, _RECALL_FETCH),
            ).fetchall()
            fact_rows = db.execute(
                "SELECT content, vec_distance_cosine(embedding, ?) AS dist "
                "FROM facts WHERE user_id = ? ORDER BY dist LIMIT ?",
                (vector, user_id, _RECALL_FETCH),
            ).fetchall()

        hits = [
            MemoryHit(text=row[0], distance=row[1], source="journal")
            for row in journal_rows
        ]
        hits += [
            MemoryHit(text=row[0], distance=row[1], source="fact")
            for row in fact_rows
        ]
        hits.sort(key=lambda hit: hit.distance)
        return hits[:_RECALL_FETCH]

    async def save(self, user_id: str, content: str, *, pinned: bool) -> None:
        """Write a durable long-term fact (issue #29).

        ``pinned`` profile facts are always injected by ``session_context``; the
        rest are recalled by semantic search. Explicit saves are not deduplicated
        (the Consolidation owns dedup).
        """
        await self._insert_fact(user_id, content, pinned=pinned)

    async def _insert_fact(
        self,
        user_id: str,
        content: str,
        *,
        pinned: bool,
        source_ids: Sequence[int] = (),
    ) -> None:
        """Single long-term write path shared by ``save`` and ``append_fact``.

        The embedding runs off the event loop. The fact and, when given, the
        ``fact_sources`` links to the Journal turns it was consolidated from are
        written in one transaction, so ``forget`` can cascade.
        """
        vector = await self._embedder.embed(content)
        db = self._get_db()
        with self._lock:
            cursor = db.execute(
                "INSERT INTO facts (embedding, user_id, content, pinned, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (json.dumps(vector), user_id, content, 1 if pinned else 0),
            )
            fact_id = cursor.lastrowid
            if fact_id is not None and source_ids:
                db.executemany(
                    "INSERT OR IGNORE INTO fact_sources "
                    "(fact_id, journal_id, user_id) VALUES (?, ?, ?)",
                    [(fact_id, journal_id, user_id) for journal_id in source_ids],
                )
            db.commit()

    async def forget(
        self, user_id: str, query: str, max_distance: float
    ) -> bool:
        """Delete the nearest fact of ``user_id`` closer than ``max_distance``.

        The match is by cosine distance over the query embedding (the distance
        must be strictly below the threshold). Deleting a fact also deletes the
        Journal turns it was consolidated from, through ``fact_sources``. Returns
        whether a fact was removed; never raises.
        """
        vector = json.dumps(await self._embedder.embed(query))
        db = self._get_db()
        with self._lock:
            row = db.execute(
                "SELECT rowid, vec_distance_cosine(embedding, ?) AS dist FROM facts "
                "WHERE user_id = ? ORDER BY dist LIMIT 1",
                (vector, user_id),
            ).fetchone()
            if row is None or row[1] >= max_distance:
                return False
            fact_id = row[0]
            journal_ids = [
                source[0]
                for source in db.execute(
                    "SELECT journal_id FROM fact_sources "
                    "WHERE user_id = ? AND fact_id = ?",
                    (user_id, fact_id),
                ).fetchall()
            ]
            db.execute(
                "DELETE FROM facts WHERE rowid = ? AND user_id = ?",
                (fact_id, user_id),
            )
            db.execute(
                "DELETE FROM fact_sources WHERE user_id = ? AND fact_id = ?",
                (user_id, fact_id),
            )
            if journal_ids:
                placeholders = ",".join("?" for _ in journal_ids)
                db.execute(
                    f"DELETE FROM journal WHERE user_id = ? "
                    f"AND rowid IN ({placeholders})",
                    [user_id, *journal_ids],
                )
                db.execute(
                    f"DELETE FROM fact_sources WHERE user_id = ? "
                    f"AND journal_id IN ({placeholders})",
                    [user_id, *journal_ids],
                )
            db.commit()
        return True

    async def journal_entries_since(
        self, user_id: str, since: datetime | None
    ) -> list[JournalEntry]:
        """Journal turns for ``user_id`` since ``since`` (all if ``None``)."""
        db = self._get_db()
        query = "SELECT rowid, role, content FROM journal WHERE user_id = ?"
        params: list[object] = [user_id]
        if since is not None:
            query += " AND created_at >= ?"
            params.append(self._format_utc(since))
        query += " ORDER BY rowid ASC"
        with self._lock:
            rows = db.execute(query, params).fetchall()
        return [
            JournalEntry(id=row[0], role=row[1], text=row[2]) for row in rows
        ]

    async def last_consolidation(self, user_id: str) -> datetime | None:
        """Instant of the last Consolidation for ``user_id``, or ``None``."""
        db = self._get_db()
        with self._lock:
            row = db.execute(
                "SELECT value FROM consolidation_state WHERE key = ?",
                (self._consolidation_key(user_id),),
            ).fetchone()
        if row is None:
            return None
        return datetime.strptime(row[0], _UTC_FORMAT).replace(tzinfo=timezone.utc)

    async def mark_consolidated(self, user_id: str, when: datetime) -> None:
        """Record that a Consolidation ran at ``when`` (idempotent per user)."""
        db = self._get_db()
        with self._lock:
            db.execute(
                "INSERT INTO consolidation_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self._consolidation_key(user_id), self._format_utc(when)),
            )
            db.commit()

    async def is_duplicate(
        self, user_id: str, text: str, max_distance: float
    ) -> bool:
        """True when the nearest long-term fact is closer than ``max_distance``."""
        vector = json.dumps(await self._embedder.embed(text))
        db = self._get_db()
        with self._lock:
            row = db.execute(
                "SELECT vec_distance_cosine(embedding, ?) AS dist FROM facts "
                "WHERE user_id = ? ORDER BY dist LIMIT 1",
                (vector, user_id),
            ).fetchone()
        if row is None:
            return False
        return row[0] < max_distance

    async def append_fact(
        self,
        user_id: str,
        text: str,
        *,
        pinned: bool = False,
        source_ids: Sequence[int] = (),
    ) -> None:
        """Write a durable long-term fact (Consolidation never pins).

        ``source_ids`` are the Journal rows the fact was extracted from: they are
        linked through ``fact_sources`` so ``forget`` can delete them too.
        """
        await self._insert_fact(
            user_id, text, pinned=pinned, source_ids=source_ids
        )

    async def purge_older_than(self, days: int | None = None) -> int:
        """Delete Journal entries older than ``days`` (default: retention)."""
        db = self._get_db()
        with self._lock:
            cursor = db.execute(
                "DELETE FROM journal WHERE created_at < ?",
                (self._cutoff(days),),
            )
            db.commit()
            return cursor.rowcount

    def _purge(self, db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM journal WHERE created_at < ?", (self._cutoff(None),))

    def _cutoff(self, days: int | None) -> str:
        retention = self._retention_days if days is None else days
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
        return self._format_utc(cutoff)

    @staticmethod
    def _format_utc(moment: datetime) -> str:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime(_UTC_FORMAT)

    @staticmethod
    def _consolidation_key(user_id: str) -> str:
        return _CONSOLIDATION_KEY.format(user_id=user_id)
