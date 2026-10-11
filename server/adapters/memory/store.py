from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import sqlite_vec

from server.core.ports import Embedder

DEFAULT_SHORT_TERM_WINDOW = 20
DEFAULT_RECENT_EVENTS_WINDOW = 20
DEFAULT_RETENTION_DAYS = 3
EMBEDDING_DIM = 384
_UTC_FORMAT = "%Y-%m-%d %H:%M:%S"

_JOURNAL_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS journal USING vec0(
    embedding float[384],
    +user_id TEXT,
    +role TEXT,
    +content TEXT,
    +created_at TEXT
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

    def _pinned_facts(self, user_id: str) -> list[str]:
        # Pinned profile facts land in issue #29; empty for now.
        return []

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
        return cutoff.strftime(_UTC_FORMAT)
