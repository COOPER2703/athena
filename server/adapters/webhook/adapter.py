from __future__ import annotations

from typing import Callable

from aiohttp import web

from server.adapters.trace import NullTraceSink, emit
from server.core.ports import TraceSink

_TRACE_SOURCE = "webhook"
_JSON_CONTENT_TYPE = "application/json"

# Route unique : la ressource exposée est l'Événement, pas le mécanisme (le
# glossaire réserve « Webhook » au transport, et évite « endpoint »).
EVENTS_PATH = "/events"
# Source par défaut quand l'émetteur n'en fournit pas : le glossaire veut une
# Source « identifiée par un nom fourni par l'émetteur », celui-ci peut l'omettre.
DEFAULT_SOURCE = "webhook"
# Miroir du plafond du noyau (``Coordinator.EVENT_MAX_CHARS``) : rejeter un corps
# que le noyau jetterait de toute façon, avant de le bufferiser en mémoire.
MAX_CONTENT_CHARS = 4096
# Garde grossière de mémoire : borne le corps avant même de le parser, bien
# au-dessus de ``MAX_CONTENT_CHARS`` pour que le rejet fin (413) reste le nôtre.
_MAX_BODY_BYTES = 64 * 1024


class WebhookAdapter:
    """Adaptateur Webhook : pousse un Événement dans le Canal via ``push``.

    Surface HTTP minimale (``POST /events``, JSON ``{"source", "content"}``). Le
    noyau seul décide où et quand juger l'Événement — l'adaptateur ne fait que
    transporter, sans file propre (ADR-0007 / ADR-0010) : ``push`` est le
    ``App.push_event`` injecté, qui dépose dans le Canal d'événements unique.

    L'observabilité passe exclusivement par le ``TraceSink`` injecté (ADR-0004).
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8766,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()
        self._push: Callable[[str, str], None] | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        """Port réellement lié (utile quand ``port=0`` en test)."""
        server = self._site._server if self._site is not None else None
        sockets = getattr(server, "sockets", None)
        if not sockets:
            raise RuntimeError("Webhook not started")
        return sockets[0].getsockname()[1]

    async def start(self, push: Callable[[str, str], None]) -> None:
        """Lie la surface HTTP et retient la couture ``push`` du noyau."""
        self._push = push
        app = web.Application(client_max_size=_MAX_BODY_BYTES)
        app.router.add_post(EVENTS_PATH, self._handle_event)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._push = None

    async def _handle_event(self, request: web.Request) -> web.Response:
        if request.content_type != _JSON_CONTENT_TYPE:
            return self._error(415, "Content-Type must be application/json")
        try:
            payload = await request.json()
        except ValueError:
            return self._error(400, "invalid JSON body")
        if not isinstance(payload, dict):
            return self._error(400, "body must be a JSON object")

        source = payload.get("source", DEFAULT_SOURCE)
        content = payload.get("content")
        if not isinstance(source, str) or not isinstance(content, str):
            return self._error(400, "source and content must be strings")
        source = source.strip()
        content = content.strip()
        if not source or not content:
            return self._error(400, "source and content must be non-empty")
        if len(content) > MAX_CONTENT_CHARS:
            return self._error(
                413, f"content exceeds {MAX_CONTENT_CHARS} characters"
            )
        if self._push is None:
            return self._error(503, "webhook not serving")

        self._push(source, content)
        emit(
            self._trace_sink,
            _TRACE_SOURCE,
            "event_received",
            payload={"source": source, "chars": len(content)},
        )
        return web.json_response({"status": "accepted"}, status=202)

    @staticmethod
    def _error(status: int, message: str) -> web.Response:
        return web.json_response({"error": message}, status=status)
