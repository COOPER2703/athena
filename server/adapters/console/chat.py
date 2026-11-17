"""Tour texte de la Console : soumission, flux ``text/event-stream``, annulation.

Le **Tour** est l'unité de conversation écrite (ADR-0023) : une entrée, une
Connexion Live ouverte pour sa durée, une **destination** qui reçoit le texte,
l'audio et les appels de Tools, puis retour au Repos. La Console n'est pas un
Client : cet adaptateur ne voit ni le Coordinator ni l'état de Session. Il
dépose un Tour dans le Canal par la couture ``TourControl`` injectée par la
racine de composition, et reçoit les événements par la destination qu'il
implémente lui-même (``TourStreams``).

Fermer la réponse annule le Tour : toute écriture ratée ou toute fin anormale
dépose un ``TourCanceled`` dans le Canal, jamais un accès direct au noyau.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any, Callable, Mapping

from aiohttp import web

from server.adapters.console.errors import error_response
from server.core.ports import InstallationStatus, TourControl

# Type de contenu du flux. Le proxy inverse doit désactiver son tampon
# (``X-Accel-Buffering: no``), sinon les événements n'arrivent qu'à la fin.
SSE_CONTENT_TYPE = "text/event-stream"
# Un Tour porte un message écrit : la borne évite d'inonder la Connexion Live et
# le Journal d'un seul envoi. Même ordre de grandeur qu'un Événement.
MAX_MESSAGE_CHARS = 4096
# Cadence du battement : c'est aussi ce qui fait apparaître une réponse fermée
# par le navigateur (l'écriture échoue). Court, parce qu'il borne le délai
# d'annulation d'un Tour que personne n'écoute plus.
HEARTBEAT_SECONDS = 0.25
# Audio descendant : PCM 16 bits mono 24 kHz, le format que le client existant
# joue déjà (aucune conversion côté navigateur).
AUDIO_MIME_TYPE = "audio/pcm;rate=24000"
# Événements qui closent le flux : au-delà, la réponse rend la main.
TERMINAL_EVENTS = frozenset({"done", "error", "busy"})
_FAILURE_MESSAGES = {
    "llm_failed": "Le modèle n'a pas pu répondre. Réessayez dans un instant.",
    "tour_failed": "Le Tour s'est interrompu. Réessayez dans un instant.",
}
_BUSY_MESSAGE = (
    "Athena est déjà en train de répondre. Réessayez quand elle aura terminé."
)


def _sse(event: str, data: Mapping[str, object]) -> bytes:
    """Une trame SSE : nom d'événement et charge JSON sur une seule ligne."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


class TourStream:
    """File d'événements d'un Tour : une réponse HTTP, un flux, zéro attente."""

    def __init__(self, tour_id: str) -> None:
        self.tour_id = tour_id
        self._events: asyncio.Queue[tuple[str, Mapping[str, object]]] = (
            asyncio.Queue()
        )

    def push(self, event: str, data: Mapping[str, object]) -> None:
        self._events.put_nowait((event, data))

    async def next(self, timeout: float) -> tuple[str, Mapping[str, object]]:
        return await asyncio.wait_for(self._events.get(), timeout)


class TourStreams:
    """Destination de Tour (port du noyau) et registre des flux ouverts.

    Toutes les méthodes du port sont **synchrones et non bloquantes** : elles
    déposent dans la file asyncio de la réponse et rendent la main, pour que le
    noyau ne dépende jamais d'un client HTTP. Un Tour inconnu (flux déjà fermé,
    Tour refusé) est ignoré sans lever.
    """

    def __init__(self) -> None:
        self._streams: dict[str, TourStream] = {}

    def open(self, tour_id: str) -> TourStream:
        stream = TourStream(tour_id)
        self._streams[tour_id] = stream
        return stream

    def close(self, tour_id: str) -> None:
        self._streams.pop(tour_id, None)

    def _push(
        self, tour_id: str, event: str, data: Mapping[str, object]
    ) -> None:
        stream = self._streams.get(tour_id)
        if stream is not None:
            stream.push(event, data)

    # --- port ``TourDestination`` -------------------------------------------

    def text(self, tour_id: str, text: str) -> None:
        self._push(tour_id, "text", {"text": text})

    def audio(self, tour_id: str, data: bytes) -> None:
        self._push(
            tour_id,
            "audio",
            {
                "data": base64.b64encode(data).decode("ascii"),
                "mime_type": AUDIO_MIME_TYPE,
            },
        )

    def tool(
        self,
        tour_id: str,
        name: str,
        arguments: Mapping[str, object],
        status: str,
    ) -> None:
        self._push(
            tour_id,
            "tool",
            {"name": name, "arguments": dict(arguments), "status": status},
        )

    def done(self, tour_id: str, reason: str) -> None:
        self._push(tour_id, "done", {"reason": reason})

    def failed(self, tour_id: str, code: str) -> None:
        self._push(
            tour_id,
            "error",
            {
                "error": code,
                "message": _FAILURE_MESSAGES.get(
                    code, _FAILURE_MESSAGES["tour_failed"]
                ),
            },
        )

    def busy(self, tour_id: str) -> None:
        self._push(tour_id, "busy", {"error": "busy", "message": _BUSY_MESSAGE})


class _BadMessage(Exception):
    """Corps de requête invalide : traduit en erreur d'API à champ."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _message_text(body: Any) -> str:
    if not isinstance(body, dict):
        raise _BadMessage("Le corps doit être un objet JSON.")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise _BadMessage("Le message ne peut pas être vide.")
    text = text.strip()
    if len(text) > MAX_MESSAGE_CHARS:
        raise _BadMessage(
            f"Le message dépasse {MAX_MESSAGE_CHARS} caractères."
        )
    return text


class ChatEndpoint:
    """Route ``POST /api/chat`` : un Tour, un flux, une annulation.

    La route reste **fermée** tant que l'Installation n'est pas terminée
    (``409 installation_incomplete``, ADR-0025) : sans clé Gemini, aucun Tour ne
    peut exister. Le refus « Athena est occupée » n'est pas une erreur HTTP : il
    arrive dans le flux par l'événement ``busy``, comme le prévoit le contrat.
    """

    def __init__(
        self,
        *,
        installation: Callable[[], InstallationStatus],
        tours: TourStreams | None = None,
    ) -> None:
        self._installation = installation
        self._tours = tours if tours is not None else TourStreams()
        self._control: TourControl | None = None

    @property
    def destination(self) -> TourStreams:
        """La destination injectée dans la racine de composition (port du noyau)."""
        return self._tours

    def bind(self, control: TourControl | None) -> None:
        """Reçoit la couture du noyau au démarrage de la surface."""
        self._control = control

    async def handle(self, request: web.Request) -> web.StreamResponse:
        status: InstallationStatus = self._installation()
        if not status.installation_complete:
            return error_response(
                "installation_incomplete",
                409,
                message=(
                    "L'Installation n'est pas terminée : enregistrez d'abord "
                    "une clé Gemini dans les Réglages."
                ),
                fields={"GEMINI_API_KEY": "Clé Gemini manquante."},
            )
        control = self._control
        if control is None:
            return error_response(
                "not_implemented",
                501,
                message="La Conversation sera disponible dans une prochaine version.",
            )
        try:
            text = _message_text(await request.json())
        except _BadMessage as invalid:
            return error_response(
                "invalid_message",
                400,
                message=invalid.message,
                fields={"text": invalid.message},
            )
        except (ValueError, TypeError):
            return error_response(
                "invalid_message",
                400,
                message="Le corps de la requête doit être un objet JSON.",
                fields={"text": "Corps JSON invalide."},
            )

        tour_id = uuid.uuid4().hex
        stream = self._tours.open(tour_id)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": f"{SSE_CONTENT_TYPE}; charset=utf-8",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)
        completed = False
        try:
            # Le Tour entre dans le Canal **après** la préparation de la
            # réponse : aucun événement ne peut précéder le flux.
            control.submit_tour(tour_id, text)
            while True:
                try:
                    event, data = await stream.next(HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # Battement : garde la connexion vivante et révèle une
                    # réponse fermée par le navigateur (l'écriture échoue).
                    await response.write(b": keep-alive\n\n")
                    continue
                await response.write(_sse(event, data))
                if event in TERMINAL_EVENTS:
                    completed = True
                    return response
        except asyncio.CancelledError:
            raise
        except ConnectionResetError:
            # Le navigateur a fermé l'onglet : le Tour est annulé, jamais
            # laissé orphelin.
            completed = False
        finally:
            self._tours.close(tour_id)
            if not completed:
                control.cancel_tour(tour_id)
        return response
