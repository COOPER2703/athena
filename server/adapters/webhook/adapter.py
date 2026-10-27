from __future__ import annotations

from typing import Callable

from aiohttp import web

from server.adapters.event_body import (
    INVALID_CONTENT_TYPE,
    INVALID_JSON_BODY,
    JSON_CONTENT_TYPE,
    MAX_BODY_BYTES,
    Rejection,
    emit_event_received,
    not_serving,
    validate_event,
)
from server.adapters.http_auth import bearer_secret
from server.adapters.trace import NullTraceSink
from server.core.ports import AuthService, TraceSink

_TRACE_SOURCE = "webhook"

# Le Webhook n'accepte qu'un Jeton de nature ``source`` : un Jeton ``client``
# appartient au transport et ne doit pas ouvrir cette surface (spec #75).
_TOKEN_KIND = "source"
_UNAUTHORIZED = Rejection(401, "missing or invalid access token")
# Le corps peut encore déclarer une Source, mais elle ne peut pas contredire
# celle du Jeton : l'usurpation est refusée explicitement (403), sans poussée.
_SOURCE_MISMATCH = Rejection(403, "source does not match the token source")

# Route unique : la ressource exposée est l'Événement, pas le mécanisme (le
# glossaire réserve « Webhook » au transport, et évite « endpoint »).
EVENTS_PATH = "/events"
# Source par défaut quand l'émetteur n'en fournit pas : le glossaire veut une
# Source « identifiée par un nom fourni par l'émetteur », celui-ci peut l'omettre.
# Avec un Jeton injecté, c'est la Source du Jeton qui sert de défaut.
DEFAULT_SOURCE = "webhook"


class WebhookAdapter:
    """Adaptateur Webhook : pousse un Événement dans le Canal via ``push``.

    Surface HTTP minimale (``POST /events``, JSON ``{"source", "content"}``). Le
    noyau seul décide où et quand juger l'Événement — l'adaptateur ne fait que
    transporter, sans file propre (ADR-0007 / ADR-0010) : ``push`` est le
    ``App.push_event`` injecté, qui dépose dans le Canal d'événements unique.

    Quand un ``AuthService`` est injecté, chaque requête exige un Jeton d'accès
    de nature ``source`` dans ``Authorization: Bearer``, validé **avant** toute
    lecture du corps. Le Jeton fait autorité : la Source de l'Événement est la
    sienne, et un corps qui en déclare une autre est refusé explicitement
    (spec #75, ticket #80).

    L'observabilité passe exclusivement par le ``TraceSink`` injecté (ADR-0004).
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8766,
        trace_sink: TraceSink | None = None,
        auth: AuthService | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()
        # La couture d'authentification est injectée par la racine de
        # composition. Sans elle, la surface reste ouverte : c'est le mode des
        # tests unitaires de l'adaptateur, jamais celui de ``build_app``.
        self._auth = auth
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
        app = web.Application(client_max_size=MAX_BODY_BYTES)
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
        if self._auth is not None:
            token_source = await self._authenticate(request)
            if token_source is None:
                return self._reject(
                    _UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"}
                )
        else:
            token_source = None

        if request.content_type != JSON_CONTENT_TYPE:
            return self._reject(INVALID_CONTENT_TYPE)
        try:
            payload = await request.json()
        except ValueError:
            return self._reject(INVALID_JSON_BODY)

        outcome = validate_event(
            payload, default_source=token_source or DEFAULT_SOURCE
        )
        if isinstance(outcome, Rejection):
            return self._reject(outcome)
        if token_source is not None and outcome.source != token_source:
            return self._reject(_SOURCE_MISMATCH)
        if self._push is None:
            return self._reject(not_serving("webhook"))

        # Le Jeton fait autorité : la Source poussée est la sienne, jamais celle
        # qu'un émetteur aurait déclarée. Le contrôle ci-dessus garantit qu'un
        # Jeton présent et la Source retenue sont égaux.
        self._push(outcome.source, outcome.content)
        emit_event_received(self._trace_sink, _TRACE_SOURCE, outcome)
        return web.json_response({"status": "accepted"}, status=202)

    async def _authenticate(self, request: web.Request) -> str | None:
        """Valide le Jeton et rend sa Source, ou ``None`` si le refus est total.

        Le Jeton est validé **avant** toute lecture du corps : un inconnu ne
        fait pas parser de JSON, et n'apprend rien de la validation du corps.
        """
        assert self._auth is not None
        secret = bearer_secret(request.headers.get("Authorization"))
        if secret is None:
            return None
        token = await self._auth.authenticate_token(secret, _TOKEN_KIND)
        if token is None or not token.source:
            return None
        await self._auth.mark_token_used(token.id)
        return token.source

    @staticmethod
    def _reject(
        rejection: Rejection, *, headers: dict[str, str] | None = None
    ) -> web.Response:
        return web.json_response(
            {"error": rejection.message},
            status=rejection.status,
            headers=headers,
        )
