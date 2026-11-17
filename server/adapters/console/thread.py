"""Fil de la Conversation : lecture paginée du Journal (ticket #172, ADR-0024).

Le fil n'est **pas** un état du noyau : c'est une lecture du Journal, qui montre
les Tours texte, les transcriptions des Sessions vocales et les Annonces, chacun
marqué par son origine. La Console ne connaît ni la base ni le noyau : la racine
de composition lui injecte un ``ThreadReader``. Le curseur de pagination est
l'identifiant du Journal (``rowid``), jamais l'horodatage qui n'a qu'une seconde
de précision.
"""

from __future__ import annotations

from aiohttp import web

from server.adapters.console.contract import ENVELOPE_VERSION
from server.adapters.console.errors import error_response
from server.core.ports import ThreadReader

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _positive_int(raw: str | None, *, maximum: int | None = None) -> int | None:
    """Entier strictement positif, ou ``None`` si la valeur est invalide."""
    if raw is None:
        return None
    try:
        value = int(raw, 10)
    except ValueError:
        return None
    if value < 1:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


class ThreadRoutes:
    """Route ``GET /api/thread`` : les entrées récentes, paginées, en lecture seule."""

    def __init__(self, *, reader: ThreadReader | None = None) -> None:
        self._reader = reader

    async def handle(self, request: web.Request) -> web.Response:
        reader = self._reader
        if reader is None:
            return error_response(
                "thread_unavailable",
                503,
                message="Le fil de la Conversation n'est pas disponible.",
            )

        raw_limit = request.query.get("limit")
        limit = DEFAULT_LIMIT if raw_limit is None else _positive_int(
            raw_limit, maximum=MAX_LIMIT
        )
        if limit is None:
            return error_response(
                "invalid_limit",
                400,
                message=(
                    f"La taille de page doit être un entier entre 1 et {MAX_LIMIT}."
                ),
                fields={"limit": f"Entier attendu entre 1 et {MAX_LIMIT}."},
            )

        raw_before = request.query.get("before")
        before = None if raw_before is None else _positive_int(raw_before)
        if raw_before is not None and before is None:
            return error_response(
                "invalid_cursor",
                400,
                message="Le curseur de pagination est invalide.",
                fields={"before": "Identifiant d'entrée attendu."},
            )

        page = await reader.recent(limit=limit, before=before)
        entries = [
            {
                "id": entry.id,
                "role": entry.role,
                "origin": entry.origin,
                "text": entry.text,
                "created_at": entry.created_at,
            }
            for entry in page.entries
        ]
        return web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "entries": entries,
                "next_before": entries[0]["id"] if page.has_more and entries else None,
                "retention_days": page.retention_days,
            }
        )
