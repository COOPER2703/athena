"""Authentification applicative : Compte, Ticket Console et Jetons d'accès.

Une base SQLite **distincte** de la mémoire d'Athena (spec #75) : les Comptes,
les Tickets Console et les Jetons d'accès se sauvegardent et se restaurent
indépendamment de la mémoire. Le schéma est créé par ``CREATE TABLE IF NOT
EXISTS`` : aucune infrastructure de migration, les évolutions restent
additives.

Le secret des Tickets et des Jetons n'est **jamais** stocké : seule son
empreinte SHA-256 l'est (ce sont des secrets à haute entropie, pas des mots de
passe). Le mot de passe du Compte est haché en Argon2id ; le calcul est déporté
hors de la boucle asyncio unique (ADR-0005) par ``asyncio.to_thread``.

Comme les autres adaptateurs, ce module n'importe pas ``logging`` : le Code
d'installation et les états passent par le ``TraceSink`` injecté, donc par le
flux unique de traces et le journal (ADR-0004).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from typing import Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from server.adapters.trace import NullTraceSink, emit
from server.core.ports import (
    AccessToken,
    Account,
    AuthenticatedTicket,
    AuthError,
    IssuedAccessToken,
    IssuedTicket,
    TraceSink,
)

# Durée d'un Ticket Console : 30 jours glissants. L'expiration n'est repoussée
# qu'au plus une fois par heure, pour éviter une écriture par requête.
DEFAULT_TICKET_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_TICKET_REFRESH_SECONDS = 60 * 60
# Longueur minimale imposée côté serveur, en plus du hachage à mémoire dure.
DEFAULT_PASSWORD_MIN_LENGTH = 12
# Cinq échecs bloquent quinze minutes ; l'adresse IP n'est pas la clé (derrière
# un proxy elle serait constante ou contournable par en-tête forgé).
DEFAULT_LOGIN_MAX_ATTEMPTS = 5
DEFAULT_LOGIN_BLOCK_SECONDS = 15 * 60
DEFAULT_SETUP_MAX_ATTEMPTS = 5
# Dernière utilisation d'un Jeton : écrite au plus une fois par minute et par
# Jeton, pour ne pas dégrader le transport avec de l'observabilité (spec #75).
DEFAULT_LAST_USED_RESOLUTION_SECONDS = 60

# Préfixe lisible d'un Jeton d'accès : ``ath_`` rend une fuite accidentelle
# détectable par une simple recherche de chaîne (spec #75, « Secrets »).
TOKEN_PREFIX = "ath_"
# Le préfixe affichable garde ``ath_`` et huit caractères du secret : assez pour
# reconnaître un Jeton dans la liste, trop peu pour le reconstituer.
TOKEN_DISPLAY_PREFIX_LENGTH = len(TOKEN_PREFIX) + 8
# 256 bits d'entropie, comme les secrets de Ticket Console.
TOKEN_SECRET_BYTES = 32
TOKEN_KINDS = frozenset({"client", "source"})

TRACE_SOURCE = "auth"

_ACCOUNTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL
)
"""

_CONSOLE_TICKETS_SCHEMA = """
CREATE TABLE IF NOT EXISTS console_tickets (
    id            TEXT PRIMARY KEY,
    account_id    TEXT NOT NULL,
    secret_hash   TEXT NOT NULL UNIQUE,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    last_activity REAL NOT NULL,
    revoked_at    REAL
)
"""

# Schéma créé dès maintenant pour que les tickets #78/#79/#80 s'ajoutent sans
# migration. Seules les méthodes de #76 sont exposées.
_ACCESS_TOKENS_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_tokens (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('client', 'source')),
    source       TEXT,
    prefix       TEXT NOT NULL,
    secret_hash  TEXT NOT NULL UNIQUE,
    created_at   REAL NOT NULL,
    last_used_at REAL,
    revoked_at   REAL
)
"""


def _digest(secret: str) -> str:
    """Empreinte SHA-256 d'un secret à haute entropie (jamais Argon2)."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _new_token_secret() -> str:
    """Secret de Jeton : 256 bits, préfixé ``ath_`` pour être reconnaissable."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_SECRET_BYTES)


def _token_from_row(row: tuple[object, ...]) -> AccessToken:
    """Traduit une ligne ``access_tokens`` en Jeton public (sans empreinte)."""
    return AccessToken(
        id=str(row[0]),
        name=str(row[1]),
        kind=str(row[2]),
        source=None if row[3] is None else str(row[3]),
        prefix=str(row[4]),
        created_at=float(row[5]),
        last_used_at=None if row[6] is None else float(row[6]),
        revoked_at=None if row[7] is None else float(row[7]),
    )


class _Attempts:
    """Compteur d'échecs en mémoire, par clé (nom d'utilisateur ou installation)."""

    __slots__ = ("failures", "locked_until")

    def __init__(self) -> None:
        self.failures = 0
        self.locked_until = 0.0


class SqliteAuthStore:
    """Implémentation SQLite du port ``AuthService`` (spec #75).

    Le store est synchrone côté base (comme ``SqliteMemoryStore``) et expose des
    coroutines pour les opérations que la surface appelle : seuls le hachage et
    la vérification Argon2 sont déportés hors de la boucle. La connexion est
    paresseuse : construire le store ne touche pas le disque.
    """

    def __init__(
        self,
        *,
        db_path: str,
        trace_sink: TraceSink | None = None,
        now: Callable[[], float] = time.time,
        ticket_ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
        ticket_refresh_seconds: int = DEFAULT_TICKET_REFRESH_SECONDS,
        password_min_length: int = DEFAULT_PASSWORD_MIN_LENGTH,
        login_max_attempts: int = DEFAULT_LOGIN_MAX_ATTEMPTS,
        login_block_seconds: float = DEFAULT_LOGIN_BLOCK_SECONDS,
        setup_max_attempts: int = DEFAULT_SETUP_MAX_ATTEMPTS,
        last_used_resolution_seconds: float = DEFAULT_LAST_USED_RESOLUTION_SECONDS,
    ) -> None:
        self._db_path = db_path
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()
        self._now = now
        self._ticket_ttl = ticket_ttl_seconds
        self._ticket_refresh = ticket_refresh_seconds
        self._password_min_length = password_min_length
        self._login_max_attempts = login_max_attempts
        self._login_block = login_block_seconds
        self._setup_max_attempts = setup_max_attempts
        self._last_used_resolution = last_used_resolution_seconds
        # Paramètres par défaut de la bibliothèque : Argon2id, mémoire dure.
        self._hasher = PasswordHasher()
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._attempts: dict[str, _Attempts] = {}
        # Code d'installation à usage unique, en mémoire seulement : il ne
        # survit ni au redémarrage ni à la création du Compte.
        self._installation_code: str | None = None

    @property
    def setup_required(self) -> bool:
        with self._lock:
            row = self._get_db().execute("SELECT COUNT(*) FROM accounts").fetchone()
        return int(row[0]) == 0

    @property
    def ticket_ttl(self) -> int:
        return self._ticket_ttl

    @property
    def password_min_length(self) -> int:
        return self._password_min_length

    def announce_installation_code(self) -> str | None:
        """Émet le Code d'installation, une fois par démarrage, sans Compte."""
        if not self.setup_required:
            return None
        if self._installation_code is None:
            self._installation_code = secrets.token_urlsafe(24)
            emit(
                self._trace_sink,
                TRACE_SOURCE,
                "InstallationCode",
                payload={"code": self._installation_code},
            )
        return self._installation_code

    async def install(
        self, code: str, username: str, password: str
    ) -> IssuedTicket:
        # Dès qu'un Compte existe, l'installation est fermée pour de bon : le
        # contrôle passe avant le code, donc un code encore en mémoire ne peut
        # pas rouvrir la porte.
        if not self.setup_required:
            raise AuthError("already_installed")

        now = self._now()
        self._check_attempts(_SETUP_KEY, now, self._setup_max_attempts)

        expected = self._installation_code
        if expected is None or not hmac.compare_digest(str(code), expected):
            self._record_failure(
                _SETUP_KEY, now, self._setup_max_attempts, self._login_block
            )
            self._raise_if_locked(_SETUP_KEY, now)
            raise AuthError("invalid_code")

        username = username.strip()
        if not username:
            raise AuthError("invalid_username")
        if len(password) < self._password_min_length:
            raise AuthError("weak_password")

        password_hash = await asyncio.to_thread(self._hasher.hash, password)
        ticket = self._install_account(username, password_hash, now)
        self._installation_code = None
        self._attempts.pop(_SETUP_KEY, None)
        return ticket

    async def login(self, username: str, password: str) -> IssuedTicket:
        now = self._now()
        key = username.strip()
        self._check_attempts(key, now, self._login_max_attempts)

        row = self._account_row(key)
        if row is None:
            # Même coût qu'une vérification : un utilisateur inconnu ne se
            # distingue pas d'un mot de passe faux, y compris au chronomètre.
            await asyncio.to_thread(self._hasher.hash, password)
            self._reject_credentials(key, now)

        account_id, stored_hash, stored_username = row
        try:
            await asyncio.to_thread(self._hasher.verify, stored_hash, password)
        except (VerificationError, InvalidHashError):
            self._reject_credentials(key, now)

        self._attempts.pop(key, None)
        return self._open_ticket(Account(id=account_id, username=stored_username), now)

    async def authenticate_ticket(self, secret: str) -> AuthenticatedTicket | None:
        now = self._now()
        digest = _digest(secret)
        with self._lock:
            db = self._get_db()
            row = db.execute(
                "SELECT id, account_id, expires_at, last_activity"
                " FROM console_tickets"
                " WHERE secret_hash = ? AND revoked_at IS NULL",
                (digest,),
            ).fetchone()
            if row is None:
                return None
            ticket_id, account_id, expires_at, last_activity = row
            if float(expires_at) <= now:
                return None

            refreshed = False
            if now - float(last_activity) >= self._ticket_refresh:
                expires_at = now + self._ticket_ttl
                db.execute(
                    "UPDATE console_tickets SET last_activity = ?, expires_at = ?"
                    " WHERE id = ?",
                    (now, expires_at, ticket_id),
                )
                db.commit()
                refreshed = True

            account = db.execute(
                "SELECT id, username FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if account is None:
            return None
        return AuthenticatedTicket(
            account=Account(id=account[0], username=account[1]),
            expires_at=float(expires_at),
            refreshed=refreshed,
        )

    async def revoke_ticket(self, secret: str) -> None:
        now = self._now()
        with self._lock:
            db = self._get_db()
            db.execute(
                "UPDATE console_tickets SET revoked_at = ?"
                " WHERE secret_hash = ? AND revoked_at IS NULL",
                (now, _digest(secret)),
            )
            db.commit()

    # --- Jetons d'accès (spec #75, ticket #78) -----------------------------

    async def list_tokens(self) -> list[AccessToken]:
        """Liste les Jetons, du plus récent au plus ancien, sans secret."""
        with self._lock:
            rows = self._get_db().execute(
                "SELECT id, name, kind, source, prefix, created_at,"
                " last_used_at, revoked_at"
                " FROM access_tokens ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
        return [_token_from_row(row) for row in rows]

    async def create_token(
        self, *, name: str, kind: str, source: str | None = None
    ) -> IssuedAccessToken:
        """Crée un Jeton et renvoie son secret en clair, une seule fois.

        La source est l'autorité du Webhook : elle est obligatoire pour la
        nature ``source`` et interdite pour ``client``, pour qu'un Jeton ne
        puisse pas changer de surface à l'usage.
        """
        name = name.strip()
        if not name:
            raise AuthError("invalid_token_name")
        if kind not in TOKEN_KINDS:
            raise AuthError("invalid_kind")
        source = source.strip() if isinstance(source, str) else None
        if kind == "source":
            if not source:
                raise AuthError("invalid_source")
        elif source:
            raise AuthError("invalid_source")

        now = self._now()
        secret = _new_token_secret()
        token = AccessToken(
            id=secrets.token_hex(16),
            name=name,
            kind=kind,
            source=source if kind == "source" else None,
            prefix=secret[:TOKEN_DISPLAY_PREFIX_LENGTH],
            created_at=now,
        )
        with self._lock:
            db = self._get_db()
            db.execute(
                "INSERT INTO access_tokens"
                " (id, name, kind, source, prefix, secret_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    token.id,
                    token.name,
                    token.kind,
                    token.source,
                    token.prefix,
                    _digest(secret),
                    now,
                ),
            )
            db.commit()
        return IssuedAccessToken(token=token, secret=secret)

    async def revoke_token(self, token_id: str) -> AccessToken | None:
        """Révoque un Jeton ; ``None`` si l'identifiant est inconnu.

        La première révocation fait foi : révoquer deux fois garde la date
        d'origine. La validation suivante refuse le Jeton, sans redémarrage.
        """
        now = self._now()
        with self._lock:
            db = self._get_db()
            db.execute(
                "UPDATE access_tokens SET revoked_at = ?"
                " WHERE id = ? AND revoked_at IS NULL",
                (now, token_id),
            )
            db.commit()
            row = db.execute(
                "SELECT id, name, kind, source, prefix, created_at,"
                " last_used_at, revoked_at"
                " FROM access_tokens WHERE id = ?",
                (token_id,),
            ).fetchone()
        return None if row is None else _token_from_row(row)

    async def authenticate_token(
        self, secret: str, kind: str
    ) -> AccessToken | None:
        """Résout un secret en Jeton de la nature demandée, ou ``None``.

        Un Jeton inconnu, révoqué ou d'une autre nature est refusé : la
        révocation est donc réelle dès la requête suivante, sans cache.
        """
        if not secret or kind not in TOKEN_KINDS:
            return None
        with self._lock:
            row = self._get_db().execute(
                "SELECT id, name, kind, source, prefix, created_at,"
                " last_used_at, revoked_at"
                " FROM access_tokens"
                " WHERE secret_hash = ? AND kind = ? AND revoked_at IS NULL",
                (_digest(secret), kind),
            ).fetchone()
        return None if row is None else _token_from_row(row)

    async def mark_token_used(self, token_id: str) -> None:
        """Enregistre la dernière utilisation, au plus une fois par minute.

        Le contrôle et l'écriture sont une seule requête : deux appels
        concurrents ne peuvent pas produire deux écritures rapprochées. Un
        Jeton révoqué n'est plus marqué.
        """
        now = self._now()
        with self._lock:
            db = self._get_db()
            db.execute(
                "UPDATE access_tokens SET last_used_at = ?"
                " WHERE id = ? AND revoked_at IS NULL"
                " AND (last_used_at IS NULL OR last_used_at <= ?)",
                (now, token_id, now - self._last_used_resolution),
            )
            db.commit()

    # --- Mécanique interne -------------------------------------------------

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            db = sqlite3.connect(self._db_path, check_same_thread=False)
            db.execute(_ACCOUNTS_SCHEMA)
            db.execute(_CONSOLE_TICKETS_SCHEMA)
            db.execute(_ACCESS_TOKENS_SCHEMA)
            db.commit()
            self._db = db
        return self._db

    def _account_row(self, username: str) -> tuple[str, str, str] | None:
        with self._lock:
            row = self._get_db().execute(
                "SELECT id, password_hash, username FROM accounts WHERE username = ?",
                (username,),
            ).fetchone()
        return None if row is None else (row[0], row[1], row[2])

    def _install_account(
        self, username: str, password_hash: str, now: float
    ) -> IssuedTicket:
        account = Account(id=secrets.token_hex(16), username=username)
        with self._lock:
            db = self._get_db()
            # Le verrou couvre contrôle et insertion : deux installations
            # concurrentes ne peuvent pas créer deux Comptes.
            if int(db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]) != 0:
                raise AuthError("already_installed")
            db.execute(
                "INSERT INTO accounts (id, username, password_hash, created_at)"
                " VALUES (?, ?, ?, ?)",
                (account.id, account.username, password_hash, now),
            )
            ticket = self._insert_ticket(db, account, now)
            db.commit()
        return ticket

    def _open_ticket(self, account: Account, now: float) -> IssuedTicket:
        with self._lock:
            db = self._get_db()
            ticket = self._insert_ticket(db, account, now)
            db.commit()
        return ticket

    def _insert_ticket(
        self, db: sqlite3.Connection, account: Account, now: float
    ) -> IssuedTicket:
        secret = secrets.token_urlsafe(32)
        ticket_id = secrets.token_hex(16)
        expires_at = now + self._ticket_ttl
        db.execute(
            "INSERT INTO console_tickets"
            " (id, account_id, secret_hash, created_at, expires_at, last_activity)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ticket_id, account.id, _digest(secret), now, expires_at, now),
        )
        return IssuedTicket(
            account=account,
            ticket_id=ticket_id,
            secret=secret,
            expires_at=expires_at,
        )

    def _reject_credentials(self, key: str, now: float) -> None:
        self._record_failure(key, now, self._login_max_attempts, self._login_block)
        self._raise_if_locked(key, now)
        raise AuthError("invalid_credentials")

    def _check_attempts(self, key: str, now: float, limit: int) -> None:
        state = self._attempts.get(key)
        if state is None:
            return
        if state.locked_until > now:
            raise AuthError(
                "too_many_attempts", retry_after=state.locked_until - now
            )
        if state.locked_until != 0.0:
            # Le délai est écoulé : le blocage se lève seul.
            self._attempts.pop(key, None)

    def _record_failure(
        self, key: str, now: float, limit: int, block_seconds: float
    ) -> None:
        state = self._attempts.setdefault(key, _Attempts())
        state.failures += 1
        if state.failures >= limit:
            state.locked_until = now + block_seconds

    def _raise_if_locked(self, key: str, now: float) -> None:
        state = self._attempts.get(key)
        if state is not None and state.locked_until > now:
            raise AuthError(
                "too_many_attempts", retry_after=state.locked_until - now
            )


_SETUP_KEY = "\x00setup"
