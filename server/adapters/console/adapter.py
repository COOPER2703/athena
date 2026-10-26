"""Adaptateur Console web : instantané de lecture et flux d'observation.

La Console web n'est **pas** un Client (GLOSSARY) : elle ne parle pas le
Protocole partagé, ne s'enregistre pas et ne reçoit pas d'audio. Cet adaptateur
sert une API JSON distincte, sur son propre port, lié à ``127.0.0.1`` par
défaut et sans authentification en v1 (ADR-0003, ADR-0016).

Il ne connaît ni le Coordinator ni ``SessionState`` : la racine de composition
lui injecte une callable ``snapshot`` qui rend un dict déjà sérialisable en
JSON. L'instantané est construit au moment de la requête, donc il reflète l'état
vivant sans qu'aucun port du noyau ne soit créé pour la Console.
"""

from __future__ import annotations

from typing import Callable, Mapping

from aiohttp import web

# Enveloppe versionnée du contrat JSON : les clients existants peuvent ignorer
# les champs qu'ils ne connaissent pas, et un changement cassant incrémente
# cette version (ADR-0002, contrat de la console).
ENVELOPE_VERSION = 1
# Préfixe réservé à l'API JSON. Le repli sous ce préfixe renvoie du JSON ; le
# reste du serveur appartient à la SPA (ticket suivant).
API_PREFIX = "/api"
STATUS_PATH = "/api/status"


class ConsoleAdapter:
    """Surface d'observation en lecture seule, alimentée par ``snapshot``.

    ``snapshot`` est la couture de la racine de composition : l'adaptateur
    l'appelle à chaque requête, sans retenir ni interpréter l'état du noyau.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8767,
    ) -> None:
        self._host = host
        self._port = port
        self._snapshot: Callable[[], Mapping[str, object]] | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        """Port réellement lié (utile quand ``port=0`` en test)."""
        server = self._site._server if self._site is not None else None
        sockets = getattr(server, "sockets", None)
        if not sockets:
            raise RuntimeError("Console not started")
        return sockets[0].getsockname()[1]

    async def start(self, snapshot: Callable[[], Mapping[str, object]]) -> None:
        """Lie la surface HTTP et retient la couture ``snapshot``."""
        self._snapshot = snapshot
        app = web.Application()
        self._register_routes(app)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._snapshot = None

    def _register_routes(self, app: web.Application) -> None:
        """Enregistre les routes de lecture.

        Les routes exactes passent avant le repli ``/api/*`` : les tickets
        suivants ajoutent ici ``/api/events`` et ``/api/stream``, sans toucher
        au reste de l'adaptateur.
        """
        app.router.add_get(STATUS_PATH, self._handle_status)
        # Tout chemin inconnu **sous /api** reste du JSON : un client de l'API
        # ne reçoit jamais une page HTML ou du texte. Le repli est enregistré
        # après les routes exactes, qui gardent la priorité.
        app.router.add_route("*", API_PREFIX, self._handle_unknown_api)
        app.router.add_route("*", f"{API_PREFIX}/{{tail:.*}}", self._handle_unknown_api)

    async def _handle_status(self, request: web.Request) -> web.Response:
        snapshot = self._snapshot() if self._snapshot is not None else {}
        return web.json_response({"v": ENVELOPE_VERSION, **snapshot})

    async def _handle_unknown_api(self, request: web.Request) -> web.Response:
        return web.json_response({"error": "not found"}, status=404)
