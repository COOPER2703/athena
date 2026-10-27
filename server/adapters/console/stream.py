"""WebSocket de traces de la Console web (``GET /api/stream``).

Le flux est unidirectionnel : le serveur envoie une unique trame ``hello``
(instantané + backlog), puis une trame ``trace`` par entrée vivante. La Console
n'écrit rien sur ce socket.

Le passage du backlog au direct est atomique : ``TraceBuffer.subscribe`` copie
le backlog et enregistre l'abonné dans la même opération synchrone, exécutée
dans la boucle asyncio unique du serveur.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Mapping

from aiohttp import web

from server.adapters.console.contract import ENVELOPE_VERSION, STREAM_PATH
from server.adapters.console.status import (
    StatusBroadcaster,
    StatusSubscription,
)
from server.adapters.console.trace_buffer import (
    TraceBuffer,
    TraceSubscription,
    trace_to_json,
)
from server.core.ports import TraceEntry


def hello_frame(
    snapshot: Mapping[str, object],
    backlog: tuple[TraceEntry, ...],
) -> dict[str, object]:
    """Trame unique d'ouverture : instantané de lecture et historique récent."""
    return {
        "v": ENVELOPE_VERSION,
        "type": "hello",
        "data": {
            "snapshot": dict(snapshot),
            "backlog": [trace_to_json(entry) for entry in backlog],
        },
    }


def trace_frame(entry: TraceEntry) -> dict[str, object]:
    """Trame vivante, y compris le marqueur de troncature (kind réservé)."""
    return {
        "v": ENVELOPE_VERSION,
        "type": "trace",
        "data": trace_to_json(entry),
    }


def status_frame(snapshot: Mapping[str, object]) -> dict[str, object]:
    """Trame différentielle : ``data`` est exactement le corps de /api/status."""
    return {
        "v": ENVELOPE_VERSION,
        "type": "status",
        "data": dict(snapshot),
    }


def stream_handler(
    buffer: TraceBuffer,
    snapshot: Callable[[], Mapping[str, object]],
    status: StatusBroadcaster | None = None,
    *,
    ticket_valid: Callable[[web.Request], Awaitable[bool]] | None = None,
    ticket_check_interval: float = 1.0,
):
    """Fabrique le handler WebSocket, lié au tampon, à l'instantané et au tick.

    ``ticket_valid`` revérifie périodiquement le Ticket Console du flux : un
    Ticket révoqué ferme un socket **déjà ouvert**, sans attendre une
    reconnexion (#77). Sans cette couture, le flux ne se referme que sur
    l'initiative du client.
    """

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return web.json_response({"error": "websocket required"}, status=400)

        websocket = web.WebSocketResponse(heartbeat=30.0)
        await websocket.prepare(request)

        # Tout ce bloc est synchrone : aucune entrée ni aucun changement d'état
        # ne peut s'intercaler entre la lecture de l'instantané et les
        # abonnements. Un abonné reçoit donc le backlog et l'état courants, puis
        # uniquement ce qui vient après — ni trou, ni doublon.
        current_snapshot = dict(snapshot())
        subscription: TraceSubscription = buffer.subscribe()
        status_subscription: StatusSubscription | None = (
            status.subscribe() if status is not None else None
        )
        if status is not None:
            # L'état part dans le `hello` : il devient la référence du tick,
            # sinon un changement antérieur à la connexion serait rejoué en
            # trame `status` identique.
            status.adopt(current_snapshot)

        async def send_traces() -> None:
            async for entry in subscription:
                await websocket.send_json(trace_frame(entry))

        async def send_status() -> None:
            assert status_subscription is not None
            async for state in status_subscription:
                await websocket.send_json(status_frame(state))

        async def drain() -> None:
            # Flux unidirectionnel : on ne lit que pour détecter la fermeture.
            async for _ in websocket:
                pass

        async def revalidate_ticket() -> None:
            assert ticket_valid is not None
            while not websocket.closed:
                await asyncio.sleep(ticket_check_interval)
                if await ticket_valid(request):
                    continue
                # 1008 (policy violation) : le client sait que son Ticket n'est
                # plus valide, la reconnexion échouera elle aussi en 401.
                await websocket.close(code=1008, message=b"Ticket Console revoked")
                return

        tasks = [asyncio.create_task(send_traces()), asyncio.create_task(drain())]
        if status_subscription is not None:
            tasks.append(asyncio.create_task(send_status()))
        if ticket_valid is not None:
            tasks.append(asyncio.create_task(revalidate_ticket()))
        try:
            await websocket.send_json(
                hello_frame(current_snapshot, subscription.backlog)
            )
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
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
            buffer.unsubscribe(subscription)
            if status_subscription is not None and status is not None:
                status.unsubscribe(status_subscription)
            if not websocket.closed:
                try:
                    # Fermeture en poignée de main, mais sans bloquer l'arrêt sur
                    # un client qui ne lit plus ses trames.
                    await asyncio.wait_for(websocket.close(), timeout=0.5)
                except (ConnectionError, asyncio.TimeoutError):
                    websocket.force_close()
        return websocket

    return handle
