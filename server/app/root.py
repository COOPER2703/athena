from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Mapping

from server.adapters.gemini import GeminiLiveAdapter
from server.adapters.trace import emit
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.config import AppConfig
from server.app.trace import build_trace_sink, install_logging_bridge
from server.core.commands import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    CloseLiveConnection,
    Command,
    OpenLiveConnection,
    SendAudioToClient,
    SendAudioToLlm,
    StartIdleTimer,
    Trace,
)
from server.core.coordinator import Coordinator
from server.core.events import Event, ShutdownRequested
from server.core.ports import TraceSink

_TRACE_SOURCE = "app"


class App:
    """Racine de composition : possède les adaptateurs et la boucle de propriété.

    Les Événements du transport, de Gemini et de l'horloge d'inactivité sont
    ramenés dans une file unique ; chaque Événement traverse le ``Coordinator``
    pur et chaque Commande retournée est routée vers son propriétaire. Une seule
    boucle asyncio, aucune tâche orpheline (ADR-0005).
    """

    def __init__(
        self,
        *,
        transport: WebSocketTransport,
        gemini: GeminiLiveAdapter,
        clock: AsyncioClock,
        trace_sink: TraceSink,
    ) -> None:
        self._transport = transport
        self._gemini = gemini
        self._clock = clock
        self._trace_sink = trace_sink
        self._coordinator = Coordinator()
        self._events: asyncio.Queue[Event] = asyncio.Queue()

    async def run(self) -> None:
        """Démarre les adaptateurs puis traite les Événements jusqu'à l'arrêt."""
        await self._transport.start()
        self._emit("started", payload={"state": self._coordinator.state.value})

        pumps = [
            asyncio.create_task(self._pump(self._transport.recv)),
            asyncio.create_task(self._pump(self._gemini.recv)),
            asyncio.create_task(self._pump(self._clock.recv)),
        ]
        try:
            await self._core_loop()
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            await self._transport.stop()
            self._clock.close()
            self._emit("stopped")

    def request_shutdown(self) -> None:
        """Dépose ``ShutdownRequested`` dans le flux, appelable depuis un signal."""
        self._events.put_nowait(ShutdownRequested())

    async def _pump(self, recv: Callable[[], Awaitable[Event]]) -> None:
        while True:
            await self._events.put(await recv())

    async def _core_loop(self) -> None:
        while True:
            event = await self._events.get()
            for command in self._coordinator.handle(event):
                await self._dispatch(command)
            if isinstance(event, ShutdownRequested):
                return

    async def _dispatch(self, command: Command) -> None:
        if isinstance(command, Trace):
            emit(
                self._trace_sink,
                command.source,
                command.kind,
                payload=command.payload,
                level=command.level,
            )
            return
        if isinstance(
            command, (OpenLiveConnection, CloseLiveConnection, SendAudioToLlm)
        ):
            await self._gemini.execute(command)
            return
        if isinstance(
            command,
            (SendAudioToClient, AnnounceSessionStarted, AnnounceSessionEnded),
        ):
            await self._transport.execute(command)
            return
        if isinstance(command, StartIdleTimer):
            self._clock.schedule(command.connection_id, command.timeout)
            return
        if isinstance(command, CancelIdleTimer):
            self._clock.cancel(command.connection_id)

    def _emit(
        self,
        kind: str,
        *,
        payload: Mapping[str, object] | None = None,
        level: str = "info",
    ) -> None:
        emit(self._trace_sink, _TRACE_SOURCE, kind, payload=payload, level=level)


def build_app(config: AppConfig) -> App:
    """Construit explicitement : config → trace → adaptateurs → Coordinator."""
    trace_sink = build_trace_sink(debug_decisions=config.debug_decisions)
    install_logging_bridge(
        trace_sink,
        level="DEBUG" if config.debug_decisions else config.log_level,
    )

    transport = WebSocketTransport(
        host=config.server.host,
        port=config.server.port,
        trace_sink=trace_sink,
    )
    gemini = GeminiLiveAdapter(
        api_key=config.gemini.api_key,
        model=config.gemini.model,
        voice=config.gemini.voice,
        trace_sink=trace_sink,
    )
    clock = AsyncioClock()

    return App(
        transport=transport,
        gemini=gemini,
        clock=clock,
        trace_sink=trace_sink,
    )
