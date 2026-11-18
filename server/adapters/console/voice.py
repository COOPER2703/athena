"""Conversation vocale de la Console : WebSocket ``GET /api/voice`` (ADR-0027).

La Console n'est pas un Client : elle **héberge** un **Client Console** le temps
d'une conversation. Le navigateur reste hors du Protocole partagé — il n'a ni
Jeton, ni Protobuf, ni ``client_id`` — et parle un contrat simple :

- **montant** : trames binaires PCM 16 bits mono 16 kHz, sans en-tête ;
- **descendant** : trames JSON versionnées ``ready``, ``transcript``, ``audio``,
  ``ended`` et ``refused``.

L'authentification est le **Ticket Console** du middleware : un handshake sans
Ticket est refusé avant toute trame, et le Ticket est revérifié pendant la
conversation comme pour le flux de Traces. La création du Client, la demande de
Session et le routage passent par la couture ``SessionControl`` injectée par la
racine de composition : cet adaptateur ne connaît ni le Coordinator ni l'état de
Session.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from typing import Awaitable, Callable, Mapping

from aiohttp import WSMsgType, web

from server.adapters.console.contract import ENVELOPE_VERSION
from server.core.ports import SessionControl

# Audio descendant : PCM 16 bits mono 24 kHz, le format que le navigateur joue
# déjà pour les Tours (aucune conversion côté page).
AUDIO_MIME_TYPE = "audio/pcm;rate=24000"
# Cadence de revérification du Ticket d'un socket déjà ouvert : même borne que
# le flux de Traces, pour qu'une révocation coupe la voix sans attendre.
TICKET_CHECK_SECONDS = 30.0
# Motifs lisibles des refus : le code reste stable et anglais, la phrase est
# destinée à la page (jamais un message brut du noyau).
_REFUSAL_MESSAGES = {
    "session_busy": (
        "Une autre conversation est déjà en cours. "
        "Réessayez quand elle aura terminé."
    ),
    "tour_busy": (
        "Athena est déjà en train de répondre. "
        "Réessayez quand elle aura terminé."
    ),
    "installation_incomplete": (
        "L'Installation n'est pas terminée : enregistrez d'abord une clé "
        "Gemini dans les Réglages."
    ),
    "voice_unavailable": (
        "La conversation vocale n'est pas disponible sur cette instance."
    ),
}
_DEFAULT_REFUSAL = "La conversation vocale n'a pas pu démarrer."


def browser_name(user_agent: str) -> str:
    """Navigateur lisible déduit de l'en-tête ``User-Agent``, côté serveur.

    L'ordre compte : ``Edg/`` et ``OPR/`` contiennent ``Chrome``, qui contient
    lui-même ``Safari``. Un en-tête absent ou inconnu rend ``Navigateur`` — le
    nom ne vient jamais de la page.
    """
    agent = user_agent or ""
    if "Firefox/" in agent:
        return "Firefox"
    if "Edg/" in agent:
        return "Edge"
    if "OPR/" in agent:
        return "Opera"
    if "Chrome/" in agent:
        return "Chrome"
    if "Safari/" in agent:
        return "Safari"
    return "Navigateur"


def console_client_name(user_agent: str) -> str:
    """Nom du Client Console : ``Console (<navigateur>)``, jamais choisi par la page."""
    return f"Console ({browser_name(user_agent)})"


def voice_frame(kind: str, data: Mapping[str, object]) -> dict[str, object]:
    """Enveloppe versionnée d'une trame vocale, identique au reste de la Console."""
    return {"v": ENVELOPE_VERSION, "type": kind, "data": dict(data)}


def refusal_message(code: str) -> str:
    return _REFUSAL_MESSAGES.get(code, _DEFAULT_REFUSAL)


class VoiceSurface:
    """File de trames d'une WebSocket : le noyau dépose, l'endpoint écrit.

    Les méthodes sont synchrones et non bloquantes, comme ``TourDestination``.
    Une trame terminale (``ended`` ou ``refused``) est envoyée une seule fois et
    ferme l'échange.
    """

    TERMINAL = frozenset({"ended", "refused"})

    def __init__(self) -> None:
        self.frames: asyncio.Queue[tuple[str, Mapping[str, object]]] = (
            asyncio.Queue()
        )
        self._ended = False

    def _push(self, kind: str, data: Mapping[str, object]) -> None:
        self.frames.put_nowait((kind, data))

    def ready(self) -> None:
        if self._ended:
            return
        self._push("ready", {})

    def transcript(self, role: str, text: str) -> None:
        if self._ended:
            return
        self._push("transcript", {"role": role, "text": text})

    def audio(self, data: bytes) -> None:
        if self._ended:
            return
        self._push(
            "audio",
            {
                "mime": AUDIO_MIME_TYPE,
                "data": base64.b64encode(data).decode("ascii"),
            },
        )

    def ended(self, reason: str) -> None:
        if self._ended:
            return
        self._ended = True
        self._push("ended", {"reason": reason})

    def refused(self, code: str) -> None:
        if self._ended:
            return
        self._ended = True
        self._push("refused", {"code": code, "message": refusal_message(code)})


def voice_handler(
    control: Callable[[], SessionControl | None],
    *,
    ticket_valid: Callable[[web.Request], Awaitable[bool]] | None = None,
    ticket_check_interval: float = TICKET_CHECK_SECONDS,
):
    """Fabrique le handler WebSocket de la conversation vocale.

    ``control`` est relu à chaque connexion : la couture du noyau est liée au
    démarrage de la surface, comme celle du Tour. ``ticket_valid`` revérifie le
    Ticket d'un socket déjà ouvert.
    """

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return web.json_response({"error": "websocket required"}, status=400)

        websocket = web.WebSocketResponse(heartbeat=30.0)
        await websocket.prepare(request)

        session = control()
        surface = VoiceSurface()
        client_id = str(uuid.uuid4())
        accepted = False
        if session is None:
            surface.refused("voice_unavailable")
        else:
            accepted = session.open_session(
                client_id=client_id,
                client_name=console_client_name(
                    request.headers.get("User-Agent", "")
                ),
                surface=surface,
            )

        if not accepted or session is None:
            # Le refus est déjà dans la file : on l'écrit avant de refermer,
            # sans jamais créer de Client ni toucher la Session en cours.
            while not surface.frames.empty():
                kind, data = surface.frames.get_nowait()
                await websocket.send_json(voice_frame(kind, data))
            await websocket.close(code=1000, message=b"refused")
            return websocket

        tasks = [
            asyncio.create_task(_send_frames(websocket, surface)),
            asyncio.create_task(_receive_audio(websocket, session, client_id)),
        ]
        if ticket_valid is not None:
            tasks.append(
                asyncio.create_task(
                    _revalidate_ticket(
                        websocket, request, ticket_valid, ticket_check_interval
                    )
                )
            )
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                error = task.exception()
                if error is not None and not isinstance(error, ConnectionError):
                    raise error
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Le Client Console est éphémère : sa disparition produit
            # ``ClientDisconnected`` et termine la Session (CLIENT_GONE).
            session.close_session(client_id)
            if not websocket.closed:
                try:
                    await asyncio.wait_for(websocket.close(), timeout=0.5)
                except (ConnectionError, asyncio.TimeoutError):
                    websocket.force_close()
        return websocket

    return handle


async def _send_frames(
    websocket: web.WebSocketResponse, surface: VoiceSurface
) -> None:
    while True:
        kind, data = await surface.frames.get()
        await websocket.send_json(voice_frame(kind, data))
        if kind in VoiceSurface.TERMINAL:
            return


async def _receive_audio(
    websocket: web.WebSocketResponse,
    session: SessionControl,
    client_id: str,
) -> None:
    """Seul le binaire montant est de l'audio : aucun autre format n'est accepté.

    Le noyau ignore l'audio reçu avant l'ouverture de la Connexion Live : la
    page n'ouvre donc le micro qu'après la trame ``ready``.
    """
    async for message in websocket:
        if message.type is WSMsgType.BINARY and message.data:
            session.send_audio(client_id, bytes(message.data))


async def _revalidate_ticket(
    websocket: web.WebSocketResponse,
    request: web.Request,
    ticket_valid: Callable[[web.Request], Awaitable[bool]],
    interval: float,
) -> None:
    while not websocket.closed:
        await asyncio.sleep(interval)
        if await ticket_valid(request):
            continue
        # 1008 (policy violation) : le Ticket n'est plus valide, la reconnexion
        # échouera elle aussi en 401.
        await websocket.close(code=1008, message=b"Ticket Console revoked")
        return
