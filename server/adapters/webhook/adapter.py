from __future__ import annotations

from typing import Callable

from aiohttp import web

from server.adapters.event_body import (
    INVALID_CONTENT_TYPE,
    INVALID_JSON_BODY,
    JSON_CONTENT_TYPE,
    Rejection,
    not_serving,
    validate_event,
)
from server.adapters.trace import NullTraceSink, emit
from server.core.ports import TraceSink

_TRACE_SOURCE = "webhook"

# Route unique : la ressource exposée est l'Événement, pas le mécanisme (le
# glossaire réserve « Webhook » au transport, et évite « endpoint »).
EVENTS_PATH = "/events"
# Source par défaut quand l'émetteur n'en fournit pas : le glossaire veut une
# Source « identifiée par un nom fourni par l'émetteur », celui-ci peut l'omettre.
DEFAULT_SOURCE = "webhook"
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
        if request.content_type != JSON_CONTENT_TYPE:
            return self._reject(INVALID_CONTENT_TYPE)
        try:
            payload = await request.json()
        except ValueError:
            return self._reject(INVALID_JSON_BODY)

        outcome = validate_event(payload, default_source=DEFAULT_SOURCE)
        if isinstance(outcome, Rejection):
            return self._reject(outcome)
        if self._push is None:
            return self._reject(not_serving("webhook"))

        self._push(outcome.source, outcome.content)
        emit(
            self._trace_sink,
            _TRACE_SOURCE,
            "event_received",
            payload={"source": outcome.source, "chars": len(outcome.content)},
        )
        return web.json_response({"status": "accepted"}, status=202)

    @staticmethod
    def _reject(rejection: Rejection) -> web.Response:
        return web.json_response(
            {"error": rejection.message}, status=rejection.status
        )
