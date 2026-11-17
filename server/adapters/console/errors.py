"""Enveloppe d'erreur unique de l'API de la Console.

Toute erreur porte un code stable, un message français et, le cas échéant, une
erreur par champ (spec #166). La Console n'affiche jamais ``message`` pour un
code inconnu : c'est le code qui est le contrat.
"""

from __future__ import annotations

import math
from typing import Mapping

from aiohttp import web

from server.adapters.console.contract import ENVELOPE_VERSION


def error_response(
    code: str,
    status: int,
    *,
    message: str,
    fields: Mapping[str, str] | None = None,
    retry_after: float | None = None,
) -> web.Response:
    body: dict[str, object] = {
        "v": ENVELOPE_VERSION,
        "error": code,
        "message": message,
    }
    if fields:
        body["fields"] = dict(fields)
    headers: dict[str, str] = {}
    if retry_after is not None:
        seconds = max(1, math.ceil(retry_after))
        body["retry_after"] = seconds
        headers["Retry-After"] = str(seconds)
    return web.json_response(body, status=status, headers=headers)
