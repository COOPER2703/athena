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

import asyncio
import os
from pathlib import Path
from typing import Callable, Mapping

from aiohttp import web

from server.adapters.console.contract import (
    API_PREFIX,
    ENVELOPE_VERSION,
    EVENTS_PATH,
    STATUS_PATH,
    STREAM_PATH,
)
from server.adapters.console.events import EventInjection
from server.adapters.console.status import (
    DEFAULT_STATUS_INTERVAL,
    StatusBroadcaster,
)
from server.adapters.console.stream import stream_handler
from server.adapters.console.trace_buffer import TraceBuffer
from server.adapters.event_body import MAX_BODY_BYTES
from server.adapters.trace import NullTraceSink
from server.core.ports import TraceSink

# Build de la SPA, produit par `make web`. Le répertoire n'existe pas
# nécessairement : l'API ne dépend pas du front (ADR-0003).
DEFAULT_STATIC_DIR = "web/dist"

# Page servie à la place du front quand le build est absent : plutôt qu'une 404
# muette, elle indique la commande à lancer. Volontairement autonome (aucun
# actif, aucun script) pour rester servie même sans le reste du build.
_EXPLANATION_PAGE = """<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8" />
    <title>Console Athena — build absent</title>
    <style>
      body {
        margin: 0;
        padding: 3rem;
        background: #101215;
        color: #d8dee9;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        line-height: 1.6;
      }
      h1 { font-size: 1.25rem; color: #8fbcbb; }
      code { color: #ebcb8b; }
      p { max-width: 60ch; }
    </style>
  </head>
  <body>
    <h1>Console Athena — build absent</h1>
    <p>
      Le build de la SPA n'a pas été trouvé. L'API JSON reste disponible sous
      <code>/api</code>, mais la page de la Console doit être construite.
    </p>
    <p>Depuis la racine du dépôt, lancez :</p>
    <p><code>make web</code></p>
    <p>ou, dans <code>web/</code> :</p>
    <p><code>npm ci &amp;&amp; npm run build</code></p>
    <p>
      Le build est ensuite servi depuis le répertoire statique de la Console
      (<code>web/dist</code> par défaut, configurable par
      <code>ATHENA_CONSOLE_STATIC_DIR</code>).
    </p>
  </body>
</html>
"""


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
        static_dir: str | os.PathLike[str] = DEFAULT_STATIC_DIR,
        trace_buffer: TraceBuffer | None = None,
        trace_sink: TraceSink | None = None,
        status_interval: float = DEFAULT_STATUS_INTERVAL,
    ) -> None:
        self._host = host
        self._port = port
        self._static_dir = Path(static_dir)
        # Cadence du tick qui pousse les changements d'instantané. Le tick est
        # une tâche de fond possédée par l'adaptateur : jamais sur le chemin
        # d'une requête (ADR-0005, ADR-0016).
        self._status_interval = status_interval
        self._status_broadcaster: StatusBroadcaster | None = None
        self._status_task: asyncio.Task[None] | None = None
        # Le tampon est alimenté par le fan-out de traces de la racine de
        # composition ; l'adaptateur ne fait que le lire. Un tampon vide par
        # défaut garde la surface utilisable (et testable) sans câblage.
        self._trace_buffer = trace_buffer if trace_buffer is not None else TraceBuffer()
        # Le flux d'observation est le flux unique (ADR-0004) : l'injection
        # d'un Événement y trace ``event_received`` comme le Webhook.
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()
        self._snapshot: Callable[[], Mapping[str, object]] | None = None
        self._event_injection: EventInjection | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def trace_buffer(self) -> TraceBuffer:
        return self._trace_buffer

    @property
    def port(self) -> int:
        """Port réellement lié (utile quand ``port=0`` en test)."""
        server = self._site._server if self._site is not None else None
        sockets = getattr(server, "sockets", None)
        if not sockets:
            raise RuntimeError("Console not started")
        return sockets[0].getsockname()[1]

    async def start(
        self,
        snapshot: Callable[[], Mapping[str, object]],
        push: Callable[[str, str], None] | None = None,
    ) -> None:
        """Lie la surface HTTP, l'instantané et la couture ``push``.

        ``push`` est le ``App.push_event`` injecté : comme le Webhook, la Console
        ne possède aucune file propre, elle dépose dans le Canal d'événements
        unique (ADR-0007). Sans couture, l'injection répond 503.
        """
        self._snapshot = snapshot
        self._event_injection = EventInjection(
            push=push, trace_sink=self._trace_sink
        )
        # Le diffuseur existe avant les routes : le handler le reçoit à la
        # construction, puis la tâche de tick est lancée après la surface.
        self._status_broadcaster = StatusBroadcaster(
            snapshot, interval=self._status_interval
        )
        app = web.Application(client_max_size=MAX_BODY_BYTES)
        self._register_routes(app)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        # Le tick démarre après la surface : il lit l'instantané sans jamais
        # s'intercaler dans une requête ni dans le traitement d'un Événement.
        self._status_task = asyncio.create_task(
            self._status_broadcaster.run(), name="console-status"
        )

    async def stop(self) -> None:
        # Arrêter le tick de fond avant de fermer les flux : plus aucun état
        # n'est diffusé pendant le démontage.
        if self._status_task is not None:
            self._status_task.cancel()
            await asyncio.gather(self._status_task, return_exceptions=True)
            self._status_task = None
        if self._status_broadcaster is not None:
            self._status_broadcaster.close()
            self._status_broadcaster = None
        # Fermer les abonnés d'abord : leur itération se termine et les
        # handlers WebSocket sortent avant que le runner ne coupe les sockets.
        self._trace_buffer.close()
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._snapshot = None
        self._event_injection = None

    def _register_routes(self, app: web.Application) -> None:
        """Enregistre les routes de lecture et d'injection.

        Les routes exactes passent avant le repli ``/api/*`` : tout chemin
        inconnu sous ce préfixe reste du JSON, et le reste appartient à la SPA.
        """
        app.router.add_get(STATUS_PATH, self._handle_status)
        # Flux de traces : WebSocket unidirectionnel, sur `/api/stream`.
        app.router.add_get(
            STREAM_PATH,
            stream_handler(
                self._trace_buffer,
                self._current_snapshot,
                self._status_broadcaster,
            ),
        )
        app.router.add_post(EVENTS_PATH, self._handle_event)
        # Tout chemin inconnu **sous /api** reste du JSON : un client de l'API
        # ne reçoit jamais une page HTML ou du texte. Le repli est enregistré
        # après les routes exactes, qui gardent la priorité.
        app.router.add_route("*", API_PREFIX, self._handle_unknown_api)
        app.router.add_route("*", f"{API_PREFIX}/{{tail:.*}}", self._handle_unknown_api)
        # Tout le reste appartient à la SPA : `/`, ses chemins connus et ses
        # chemins inconnus (page unique, sans routeur) reçoivent la même entrée.
        app.router.add_get("/", self._handle_spa)
        app.router.add_get("/{tail:.*}", self._handle_spa)

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.json_response({"v": ENVELOPE_VERSION, **self._current_snapshot()})

    def _current_snapshot(self) -> Mapping[str, object]:
        return self._snapshot() if self._snapshot is not None else {}

    async def _handle_event(self, request: web.Request) -> web.Response:
        """Délègue l'injection à la route dédiée (mêmes règles que le Webhook)."""
        assert self._event_injection is not None
        return await self._event_injection.handle(request)

    async def _handle_unknown_api(self, request: web.Request) -> web.Response:
        return web.json_response({"error": "not found"}, status=404)

    async def _handle_spa(self, request: web.Request) -> web.Response:
        """Sert le build de la SPA, son entrée, ou la page explicative.

        Le fichier demandé est cherché d'abord ; à défaut, la page unique
        reçoit tous les chemins non-API. Sans build, la page explicative prend
        la place de la SPA pour que l'utilisateur sache quoi lancer.
        """
        index = self._index_file()
        if index is None:
            return web.Response(text=_EXPLANATION_PAGE, content_type="text/html")
        static_file = self._static_file(request.match_info.get("tail", ""))
        return web.FileResponse(static_file or index)

    def _index_file(self) -> Path | None:
        """Entrée de la SPA, ou ``None`` quand aucun build n'est présent."""
        index = self._static_dir / "index.html"
        return index if index.is_file() else None

    def _static_file(self, tail: str) -> Path | None:
        """Fichier du build demandé, borné au répertoire statique.

        ``resolve`` élimine ``..`` et les liens symboliques avant la
        comparaison : un chemin qui sortirait du répertoire statique est traité
        comme inconnu et reçoit l'entrée de la SPA, jamais le fichier visé.
        """
        root = self._static_dir.resolve()
        try:
            candidate = (root / tail).resolve()
        except (OSError, ValueError):
            return None
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        return candidate
