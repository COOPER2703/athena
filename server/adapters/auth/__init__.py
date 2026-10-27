"""Module d'authentification des surfaces publiques (spec #75).

L'implémentation est indépendante de la Console : elle possède sa base SQLite
(séparée de la mémoire), le hachage Argon2id des mots de passe, les Tickets
Console et le schéma des Jetons d'accès. Les surfaces ne voient que le port
``AuthService`` du noyau (``server.core.ports``).
"""

from __future__ import annotations

from server.adapters.auth.store import SqliteAuthStore

__all__ = ["SqliteAuthStore"]
