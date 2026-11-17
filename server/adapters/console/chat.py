"""Conversation de la Console : refus serveur tant que l'Installation n'est pas finie.

Le ticket #170 ne livre pas encore le Tour : cette route existe pour que la
Conversation soit **désactivée côté serveur** pendant l'Installation, avec un
code stable que le front traduit en phrase et un bouton vers le réglage manquant.
Le ticket #172 la remplacera par le flux ``text/event-stream`` complet.
"""

from __future__ import annotations

from typing import Callable

from aiohttp import web

from server.adapters.console.errors import error_response
from server.core.ports import InstallationStatus


class ChatEndpoint:
    """Route ``POST /api/chat`` : fermée tant qu'Athena ne peut pas parler."""

    def __init__(
        self,
        *,
        installation: Callable[[], InstallationStatus],
    ) -> None:
        self._installation = installation

    async def handle(self, request: web.Request) -> web.Response:
        if not self._installation().installation_complete:
            return error_response(
                "installation_incomplete",
                409,
                message=(
                    "L'Installation n'est pas terminée : enregistrez d'abord "
                    "une clé Gemini dans les Réglages."
                ),
                fields={"GEMINI_API_KEY": "Clé Gemini manquante."},
            )
        return error_response(
            "not_implemented",
            501,
            message="La Conversation sera disponible dans une prochaine version.",
        )
