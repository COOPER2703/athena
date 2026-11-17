"""Magasin de réglages et de Secrets, à côté de la mémoire et de l'auth.

Une base SQLite **dédiée** (ADR-0025) : valeurs JSON brutes, état des Secrets,
aucune migration destructive (``CREATE TABLE IF NOT EXISTS``). La clé maître est
un fichier séparé en ``0600`` : sauvegarder le magasin sans elle rend les
Secrets illisibles, ce que le démarrage signale au lieu de le masquer.

Le module ne journalise jamais une valeur de Secret. Les traces ne portent que
le nom, l'état et la date ; une panne de déchiffrement trace
``SecretsUnreadable`` une seule fois par démarrage.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from server.adapters.settings.crypto import MasterKeyFile, seal, unseal
from server.adapters.trace import NullTraceSink, emit
from server.core.ports import SecretStatus, TraceSink
from server.core.settings import KNOWN_SECRETS

TRACE_SOURCE = "settings"

_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""

_SECRETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    name       TEXT PRIMARY KEY,
    ciphertext BLOB NOT NULL,
    updated_at REAL NOT NULL
)
"""


class SqliteSettingsStore:
    """Implémentation SQLite des ports ``SettingsStore`` et ``SecretStore``.

    La connexion est paresseuse : construire le magasin ne touche pas le disque
    et ne crée pas la clé maître. Toute écriture de Secret crée la clé si elle
    manque — c'est une action explicite de l'utilisateur, jamais un démarrage.
    """

    def __init__(
        self,
        *,
        db_path: str,
        key_path: str,
        trace_sink: TraceSink | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._db_path = db_path
        self._master = MasterKeyFile(key_path)
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()
        self._now = now
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        # Résultats de déchiffrement : évite de déchiffrer à chaque /api/session.
        self._values: dict[str, str | None] = {}
        # ``None`` = pas encore calculé ; recalculé après chaque écriture.
        self._unreadable: bool | None = None
        self._reported_unreadable = False

    @property
    def master_key_path(self) -> str:
        return str(self._master.path)

    def ensure_master_key(self) -> str | None:
        """Crée la clé maître si elle manque ; rend sa forme encodée une fois.

        L'appelant est l'Installation : c'est le seul endroit où la clé est
        exposée à l'utilisateur. Une clé déjà présente n'est jamais réaffichée.
        """
        _, encoded, created = self._master.ensure()
        if not created:
            return None
        emit(
            self._trace_sink,
            TRACE_SOURCE,
            "MasterKeyCreated",
            payload={"path": str(self._master.path)},
        )
        return encoded

    # --- Réglages ----------------------------------------------------------

    def value(self, key: str) -> str | None:
        with self._lock:
            db = self._read_db()
            if db is None:
                return None
            row = db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        stored = json.loads(str(row[0]))
        return stored if isinstance(stored, str) else str(stored)

    def values(self) -> dict[str, str]:
        with self._lock:
            db = self._read_db()
            if db is None:
                return {}
            rows = db.execute(
                "SELECT key, value FROM settings ORDER BY key"
            ).fetchall()
        values: dict[str, str] = {}
        for key, raw in rows:
            decoded = json.loads(str(raw))
            values[str(key)] = decoded if isinstance(decoded, str) else str(decoded)
        return values

    def set_value(self, key: str, value: str) -> None:
        now = self._now()
        with self._lock:
            db = self._get_db()
            db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " value = excluded.value, updated_at = excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), now),
            )
            db.commit()

    def delete_value(self, key: str) -> bool:
        with self._lock:
            db = self._get_db()
            cursor = db.execute("DELETE FROM settings WHERE key = ?", (key,))
            db.commit()
        return cursor.rowcount > 0

    # --- Secrets -----------------------------------------------------------

    @property
    def unreadable(self) -> bool:
        """Vrai si un Secret stocké ne peut plus être déchiffré (ADR-0025)."""
        with self._lock:
            return self._check_unreadable()

    def secret(self, name: str) -> str | None:
        with self._lock:
            if name in self._values:
                return self._values[name]
            db = self._read_db()
            if db is None:
                self._values[name] = None
                return None
            row = db.execute(
                "SELECT ciphertext FROM secrets WHERE name = ?", (name,)
            ).fetchone()
            value: str | None = None
            if row is not None:
                key = self._master.read()
                if key is not None:
                    value = unseal(key, name, bytes(row[0]))
            self._values[name] = value
            return value

    def statuses(self) -> Sequence[SecretStatus]:
        with self._lock:
            # Une lecture ne crée pas le magasin : une instance neuve se décrit
            # (tous les Secrets connus, aucun configuré) sans écrire de base.
            db = self._read_db()
            rows = (
                []
                if db is None
                else db.execute(
                    "SELECT name, updated_at FROM secrets ORDER BY name"
                ).fetchall()
            )
            stored = {
                str(name): float(updated_at) for name, updated_at in rows
            }
        names = list(KNOWN_SECRETS)
        names.extend(name for name in stored if name not in KNOWN_SECRETS)
        return [
            SecretStatus(
                name=name,
                configured=name in stored,
                updated_at=stored.get(name),
            )
            for name in sorted(names)
        ]

    def set_secret(self, name: str, value: str) -> SecretStatus:
        """Chiffre et enregistre un Secret, en créant la clé si elle manque."""
        key, _, created = self._master.ensure()
        if created:
            emit(
                self._trace_sink,
                TRACE_SOURCE,
                "MasterKeyCreated",
                payload={"path": str(self._master.path)},
            )
        blob = seal(key, name, value)
        now = self._now()
        with self._lock:
            db = self._get_db()
            db.execute(
                "INSERT INTO secrets (name, ciphertext, updated_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET"
                " ciphertext = excluded.ciphertext,"
                " updated_at = excluded.updated_at",
                (name, blob, now),
            )
            db.commit()
            self._values[name] = value
            self._unreadable = None
        emit(self._trace_sink, TRACE_SOURCE, "SecretStored", payload={"name": name})
        return SecretStatus(name=name, configured=True, updated_at=now)

    def delete_secret(self, name: str) -> bool:
        with self._lock:
            db = self._get_db()
            cursor = db.execute("DELETE FROM secrets WHERE name = ?", (name,))
            db.commit()
            self._values.pop(name, None)
            self._unreadable = None
        deleted = cursor.rowcount > 0
        if deleted:
            emit(
                self._trace_sink,
                TRACE_SOURCE,
                "SecretDeleted",
                payload={"name": name},
            )
        return deleted

    # --- Mécanique interne -------------------------------------------------

    def _check_unreadable(self) -> bool:
        if self._unreadable is not None:
            return self._unreadable
        db = self._read_db()
        if db is None:
            self._unreadable = False
            return False
        rows = db.execute("SELECT name, ciphertext FROM secrets").fetchall()
        key = self._master.read() if rows else None
        unreadable = bool(rows) and (
            key is None
            or any(unseal(key, str(name), bytes(blob)) is None for name, blob in rows)
        )
        self._unreadable = unreadable
        if unreadable and not self._reported_unreadable:
            self._reported_unreadable = True
            emit(
                self._trace_sink,
                TRACE_SOURCE,
                "SecretsUnreadable",
                payload={"reason": "master_key_missing_or_invalid"},
                level="warning",
            )
        return unreadable

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            db = sqlite3.connect(self._db_path, check_same_thread=False)
            db.execute(_SETTINGS_SCHEMA)
            db.execute(_SECRETS_SCHEMA)
            db.commit()
            self._db = db
        return self._db

    def _read_db(self) -> sqlite3.Connection | None:
        """Base existante seulement : une lecture ne crée pas le magasin.

        La construction de la racine lit les Secrets (clé Gemini) avant toute
        requête. Créer ``settings.db`` à ce moment ferait écrire un processus
        qui n'a jamais rien enregistré — et, en test, dans le répertoire
        courant.
        """
        if self._db is not None:
            return self._db
        if not Path(self._db_path).is_file():
            return None
        return self._get_db()
