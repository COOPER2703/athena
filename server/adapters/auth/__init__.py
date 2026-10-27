"""Module d'authentification des surfaces publiques (spec #75).

L'implémentation est indépendante de la Console : elle possède sa base SQLite
(séparée de la mémoire), le hachage Argon2id des mots de passe, les Tickets
Console et le schéma des Jetons d'accès. Les surfaces ne voient que le port
``AuthService`` du noyau (``server.core.ports``).
"""

from __future__ import annotations

from server.adapters.auth.store import (
    DEFAULT_LAST_USED_RESOLUTION_SECONDS,
    DEFAULT_LOGIN_BLOCK_SECONDS,
    DEFAULT_LOGIN_MAX_ATTEMPTS,
    DEFAULT_PASSWORD_MIN_LENGTH,
    DEFAULT_SETUP_MAX_ATTEMPTS,
    DEFAULT_TICKET_REFRESH_SECONDS,
    DEFAULT_TICKET_TTL_SECONDS,
    TOKEN_PREFIX,
    SqliteAuthStore,
)

__all__ = [
    "DEFAULT_LAST_USED_RESOLUTION_SECONDS",
    "DEFAULT_LOGIN_BLOCK_SECONDS",
    "DEFAULT_LOGIN_MAX_ATTEMPTS",
    "DEFAULT_PASSWORD_MIN_LENGTH",
    "DEFAULT_SETUP_MAX_ATTEMPTS",
    "DEFAULT_TICKET_REFRESH_SECONDS",
    "DEFAULT_TICKET_TTL_SECONDS",
    "TOKEN_PREFIX",
    "SqliteAuthStore",
]
