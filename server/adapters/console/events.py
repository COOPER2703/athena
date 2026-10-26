"""Injection d'un Événement depuis la Console web.

La Console accepte un Événement comme le Webhook, avec **exactement** les mêmes
règles : la validation vit dans :mod:`server.adapters.event_body`, partagée par
les deux surfaces pour qu'elles ne dérivent pas (spec #62). Seule différence :
la Source par défaut est ``console``, afin de distinguer les Événements injectés
depuis le navigateur de ceux reçus par le Webhook.

L'Événement validé entre dans le Canal d'événements unique par la couture
``push`` injectée par la racine de composition (``App.push_event``) ; il est
tracé ``event_received`` dans le flux unique, jamais dans un fichier propre.
"""

from __future__ import annotations

from typing import Callable

from aiohttp import web

from server.adapters.event_body import (
    INVALID_CONTENT_TYPE,
    INVALID_JSON_BODY,
    JSON_CONTENT_TYPE,
    Rejection,
    emit_event_received,
    not_serving,
    validate_event,
)
from server.adapters.trace import NullTraceSink
from server.core.ports import TraceSink

EVENTS_PATH = "/api/events"
# La Console est une surface d'injection distincte du Webhook : ses Événements
# portent cette Source quand l'appelant n'en fournit pas.
DEFAULT_SOURCE = "console"
# Source de trace de la surface : ``event_received`` se lit donc ``console``.
TRACE_SOURCE = "console"


class EventInjection:
    """Route ``POST /api/events`` de la Console.

    ``push`` est la couture de la racine de composition. ``None`` signifie que
    la surface ne sert pas : l'injection répond alors 503, comme le Webhook.
    """

    def __init__(
        self,
        *,
        push: Callable[[str, str], None] | None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._push = push
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()

    async def handle(self, request: web.Request) -> web.Response:
        if request.content_type != JSON_CONTENT_TYPE:
            return _reject(INVALID_CONTENT_TYPE)
        try:
            payload = await request.json()
        except ValueError:
            return _reject(INVALID_JSON_BODY)

        outcome = validate_event(payload, default_source=DEFAULT_SOURCE)
        if isinstance(outcome, Rejection):
            return _reject(outcome)
        if self._push is None:
            return _reject(not_serving("console"))

        self._push(outcome.source, outcome.content)
        emit_event_received(self._trace_sink, TRACE_SOURCE, outcome)
        return web.json_response({"status": "accepted"}, status=202)


def _reject(rejection: Rejection) -> web.Response:
    return web.json_response({"error": rejection.message}, status=rejection.status)
