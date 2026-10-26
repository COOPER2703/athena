"""Règles de validation d'un corps d'Événement reçu en HTTP.

Module **pur** : aucun import d'``aiohttp``, aucune I/O. Les surfaces qui
acceptent un Événement en JSON (le Webhook, la Console web) partagent donc les
mêmes règles et les mêmes rejets, au lieu de les dupliquer puis de diverger.

L'appelant parse le corps (JSON) et fournit sa Source par défaut : le module ne
connaît ni le transport ni le nom de la surface.
"""

from __future__ import annotations

from dataclasses import dataclass

JSON_CONTENT_TYPE = "application/json"
# Miroir du plafond du noyau (``Coordinator.EVENT_MAX_CHARS``) : rejeter un
# corps que le noyau jetterait de toute façon, avant de le bufferiser.
MAX_CONTENT_CHARS = 4096


@dataclass(frozen=True)
class AcceptedEvent:
    """Événement validé, prêt pour la couture ``push`` du noyau."""

    source: str
    content: str


@dataclass(frozen=True)
class Rejection:
    """Rejet structuré : la surface le traduit en réponse HTTP."""

    status: int
    message: str


# Rejets identiques sur toutes les surfaces qui acceptent un Événement.
INVALID_CONTENT_TYPE = Rejection(415, "Content-Type must be application/json")
INVALID_JSON_BODY = Rejection(400, "invalid JSON body")


def not_serving(surface: str) -> Rejection:
    """Rejet d'une surface arrêtée, nommée comme l'émetteur la connaît."""
    return Rejection(503, f"{surface} not serving")


def validate_event(
    payload: object, *, default_source: str
) -> AcceptedEvent | Rejection:
    """Valide un payload déjà parsé et rend l'Événement ou son rejet."""
    if not isinstance(payload, dict):
        return Rejection(400, "body must be a JSON object")
    source = payload.get("source", default_source)
    content = payload.get("content")
    if not isinstance(source, str) or not isinstance(content, str):
        return Rejection(400, "source and content must be strings")
    source = source.strip()
    content = content.strip()
    if not source or not content:
        return Rejection(400, "source and content must be non-empty")
    if len(content) > MAX_CONTENT_CHARS:
        return Rejection(
            413, f"content exceeds {MAX_CONTENT_CHARS} characters"
        )
    return AcceptedEvent(source=source, content=content)
