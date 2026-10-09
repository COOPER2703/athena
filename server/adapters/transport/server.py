from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Mapping

import websockets
from websockets.asyncio.server import Server, ServerConnection, serve

from protocol import (
    Error,
    Ping,
    Pong,
    ProtocolError,
    Register,
    Registered,
    SessionEndReason,
    decode,
    encode,
)
from server.adapters.transport.translation import outbound_message, translate_inbound
from server.core.commands import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    Command,
    SendAudioToClient,
    Trace,
)
from server.core.events import ClientRegistered, Event
from server.core.ports import TraceEntry, TraceSink

_TRACE_SOURCE = "transport"


class _NullTraceSink:
    def emit(self, entry: TraceEntry) -> None:
        pass


@dataclass(frozen=True, slots=True)
class ClientConnection:
    client_id: str
    client_name: str
    platform: str
    websocket: ServerConnection


class WebSocketTransport:
    """Adaptateur WebSocket : connexions et enregistrement des Clients.

    Possède l'état des connexions (Clients branchés), jamais celui des Sessions.
    Les messages entrants sont traduits en Événements déposés dans une file
    (``recv``) ; les Commandes ciblées sont traduites en messages sortants
    (``execute``). Il n'existe aucune notion de Client actif : l'envoi est
    toujours ciblé par ``client_id``.

    L'observabilité passe exclusivement par le ``TraceSink`` injecté (ADR-0004).
    """

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8765,
        server_tools: list[str] | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._server_tools = list(server_tools or [])
        self._trace_sink: TraceSink = trace_sink or _NullTraceSink()
        self._clients: dict[str, ClientConnection] = {}
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._server: Server | None = None

    @property
    def port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("Transport not started")
        return self._server.sockets[0].getsockname()[1]

    @property
    def clients(self) -> Mapping[str, ClientConnection]:
        return dict(self._clients)

    async def start(self) -> None:
        self._server = await serve(self._handle_connection, self._host, self._port)

    async def stop(self) -> None:
        server = self._server
        if server is None:
            return
        server.close()
        await server.wait_closed()
        self._server = None

    async def recv(self) -> Event:
        return await self._events.get()

    async def execute(self, command: Command) -> None:
        if isinstance(command, Trace):
            self._trace_sink.emit(
                TraceEntry(
                    timestamp=time.time(),
                    source=command.source,
                    kind=command.kind,
                    payload=dict(command.payload),
                    level=command.level,
                )
            )
            return

        mapping = outbound_message(command)
        if mapping is None:
            return
        client_id, message = mapping
        connection = self._clients.get(client_id)
        if connection is None:
            self._emit(
                "client_not_found",
                payload={"client_id": client_id, "type": type(command).__name__},
                level="warning",
            )
            return
        await self._send(connection.websocket, message)

    async def send_audio(self, client_id: str, data: bytes) -> None:
        await self.execute(SendAudioToClient(client_id=client_id, data=data))

    async def announce_session_started(self, client_id: str, auto: bool) -> None:
        await self.execute(AnnounceSessionStarted(client_id=client_id, auto=auto))

    async def announce_session_ended(
        self, client_id: str, reason: SessionEndReason
    ) -> None:
        await self.execute(AnnounceSessionEnded(client_id=client_id, reason=reason))

    async def send_tool_call(
        self, client_id: str, tool_id: str, name: str, arguments_json: bytes
    ) -> None:
        # Réservé pour T1 : aucun routage de Tools client n'existe encore.
        self._emit(
            "tool_call_reserved",
            payload={"client_id": client_id, "tool_id": tool_id, "name": name},
            level="warning",
        )

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        connection: ClientConnection | None = None
        try:
            async for raw in websocket:
                if not isinstance(raw, bytes) or not raw:
                    self._emit(
                        "invalid_frame",
                        payload={"reason": "expected non-empty binary frame"},
                        level="warning",
                    )
                    continue
                try:
                    message = decode(raw)
                except ProtocolError as exc:
                    self._emit(
                        "protocol_error",
                        payload={"error": str(exc)},
                        level="warning",
                    )
                    continue
                try:
                    if connection is None:
                        connection = await self._register(websocket, message)
                    else:
                        await self._dispatch(connection, message)
                except Exception as exc:  # pragma: no cover - defensive isolation
                    self._emit(
                        "handler_error",
                        payload={"error": str(exc)},
                        level="error",
                    )
        except websockets.ConnectionClosed:
            pass
        finally:
            if connection is not None:
                self._cleanup(connection)

    async def _register(
        self, websocket: ServerConnection, message: object
    ) -> ClientConnection | None:
        if not isinstance(message, Register):
            self._emit(
                "message_before_register",
                payload={"type": type(message).__name__},
                level="warning",
            )
            return None

        client_id = str(uuid.uuid4())
        client_name = message.client_name or client_id
        platform = message.platform or "unknown"

        if any(other.client_name == client_name for other in self._clients.values()):
            self._emit(
                "registration_rejected",
                payload={"client_name": client_name, "reason": "duplicate_client_name"},
                level="warning",
            )
            await self._send(
                websocket,
                Error(message=f"Duplicate client name: {client_name}"),
            )
            await websocket.close(code=1008, reason="duplicate client_name")
            return None

        connection = ClientConnection(
            client_id=client_id,
            client_name=client_name,
            platform=platform,
            websocket=websocket,
        )
        self._clients[client_id] = connection
        await self._send(
            websocket,
            Registered(client_id=client_id, server_tools=self._server_tools),
        )
        await self._events.put(
            ClientRegistered(
                client_id=client_id, client_name=client_name, platform=platform
            )
        )
        self._emit(
            "client_registered",
            payload={
                "client_id": client_id,
                "client_name": client_name,
                "platform": platform,
            },
        )
        return connection

    async def _dispatch(self, connection: ClientConnection, message: object) -> None:
        if isinstance(message, Register):
            self._emit(
                "duplicate_register",
                payload={"client_id": connection.client_id},
                level="warning",
            )
            return
        if isinstance(message, Ping):
            await self._send(connection.websocket, Pong())
            return
        if isinstance(message, Pong):
            self._emit(
                "unexpected_pong",
                payload={"client_id": connection.client_id},
                level="warning",
            )
            return

        event = translate_inbound(connection.client_id, message)
        if event is None:
            self._emit(
                "unhandled_message",
                payload={
                    "client_id": connection.client_id,
                    "type": type(message).__name__,
                },
                level="warning",
            )
            return
        await self._events.put(event)

    def _cleanup(self, connection: ClientConnection) -> None:
        self._clients.pop(connection.client_id, None)
        self._emit(
            "client_disconnected",
            payload={
                "client_id": connection.client_id,
                "client_name": connection.client_name,
            },
        )

    async def _send(self, websocket: ServerConnection, message: object) -> None:
        try:
            await websocket.send(encode(message))
        except websockets.ConnectionClosed:
            self._emit("send_failed", payload={}, level="warning")

    def _emit(
        self,
        kind: str,
        *,
        payload: Mapping[str, object] | None = None,
        level: str = "info",
    ) -> None:
        self._trace_sink.emit(
            TraceEntry(
                timestamp=time.time(),
                source=_TRACE_SOURCE,
                kind=kind,
                payload=dict(payload or {}),
                level=level,
            )
        )
