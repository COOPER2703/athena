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
from typing import Callable, Mapping

from aiohttp import web

from server.adapters.console.adapter import ENVELOPE_VERSION
from server.adapters.console.trace_buffer import (
    TraceBuffer,
    TraceSubscription,
    trace_to_json,
)
from server.core.ports import TraceEntry

STREAM_PATH = "/api/stream"


def hello_frame(
    snapshot: Mapping[str, object],
    backlog: tuple[TraceEntry, ...],
) -> dict[str, object]:
    """Trame unique d'ouverture : état gelé et historique récent."""
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


def stream_handler(
    buffer: TraceBuffer,
    snapshot: Callable[[], Mapping[str, object]],
):
    """Fabrique le handler WebSocket, lié au tampon et à l'instantané."""

    async def handle(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return web.json_response({"error": "websocket required"}, status=400)

        websocket = web.WebSocketResponse(heartbeat=30.0)
        await websocket.prepare(request)
        subscription: TraceSubscription = buffer.subscribe()

        async def send() -> None:
            await websocket.send_json(hello_frame(snapshot(), subscription.backlog))
            async for entry in subscription:
                await websocket.send_json(trace_frame(entry))

        async def drain() -> None:
            # Flux unidirectionnel : on ne lit que pour détecter la fermeture.
            async for _ in websocket:
                pass

        sender = asyncio.create_task(send())
        receiver = asyncio.create_task(drain())
        try:
            done, pending = await asyncio.wait(
                {sender, receiver},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if sender in done:
                error = sender.exception()
                if error is not None and not isinstance(error, ConnectionError):
                    raise error
        finally:
            for task in (sender, receiver):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            buffer.unsubscribe(subscription)
            if not websocket.closed:
                try:
                    # Fermeture en poignée de main, mais sans bloquer l'arrêt sur
                    # un client qui ne lit plus ses trames.
                    await asyncio.wait_for(websocket.close(), timeout=0.5)
                except (ConnectionError, asyncio.TimeoutError):
                    websocket.force_close()
        return websocket

    return handle
