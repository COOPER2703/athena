from __future__ import annotations

import asyncio

from server.core.events import Event, TimerFired


class AsyncioClock:
    """Horloge d'inactivité sur la boucle unique (ADR-0005).

    ``schedule`` arme un timeout pour une connexion et ``cancel`` le désarme ;
    à l'échéance, l'horloge émet ``TimerFired`` dans sa file d'Événements,
    exactement comme un adaptateur. Aucun thread, aucun réveil hors boucle :
    les échéances sont des callbacks de la boucle courante.
    """

    def __init__(self) -> None:
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._handles: dict[str, asyncio.TimerHandle] = {}

    def schedule(self, connection_id: str, timeout: float) -> None:
        self.cancel(connection_id)
        loop = asyncio.get_running_loop()
        self._handles[connection_id] = loop.call_later(
            timeout, self._fire, connection_id
        )

    def cancel(self, connection_id: str) -> None:
        handle = self._handles.pop(connection_id, None)
        if handle is not None:
            handle.cancel()

    async def recv(self) -> Event:
        return await self._events.get()

    def close(self) -> None:
        for handle in self._handles.values():
            handle.cancel()
        self._handles.clear()

    def _fire(self, connection_id: str) -> None:
        self._handles.pop(connection_id, None)
        self._events.put_nowait(TimerFired(connection_id=connection_id))
