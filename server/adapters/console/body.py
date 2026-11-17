"""Lecture commune du corps JSON des routes de la Console.

Une seule règle pour toutes les routes en écriture : un corps qui n'est pas un
objet JSON est indistinguable d'un corps absent. Chaque route traduit ce ``None``
en son propre code d'erreur ; ce module ne décide rien d'autre.
"""

from __future__ import annotations

from typing import Mapping

from aiohttp import web


async def json_object(request: web.Request) -> Mapping[str, object] | None:
    """Corps JSON de la requête, ou ``None`` s'il est absent ou mal formé."""
    try:
        payload = await request.json()
    except ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None
