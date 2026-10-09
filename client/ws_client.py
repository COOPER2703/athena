from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Protocol

import websockets
from websockets.asyncio.client import ClientConnection

from protocol import (
    Audio,
    ClientToServer,
    Error,
    Interrupt,
    Ping,
    ProtocolError,
    Register,
    Registered,
    ServerToClient,
    SessionEnded,
    SessionStart,
    SessionStarted,
    decode,
    encode,
)

from client.config import ClientConfig

log = logging.getLogger("athena.client")

Connector = Callable[[str], Awaitable[ClientConnection]]
Sleeper = Callable[[float], Awaitable[None]]


class TransportListener(Protocol):
    """Récepteur des événements et messages décodés du transport."""

    async def on_audio(self, data: bytes) -> None: ...

    async def on_session_started(self, message: SessionStarted) -> None: ...

    async def on_session_ended(self, message: SessionEnded) -> None: ...

    async def on_error(self, message: Error) -> None: ...

    async def on_disconnect(self) -> None: ...


class Transport(Protocol):
    """Interface du transport consommée par l'orchestrateur."""

    @property
    def client_id(self) -> str | None: ...

    @property
    def is_connected(self) -> bool: ...

    async def run(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def send_audio(self, data: bytes) -> None: ...

    async def send_session_start(self) -> None: ...

    async def send_interrupt(self) -> None: ...

    def set_listener(self, listener: TransportListener) -> None: ...


class WsTransport:
    """Transport WebSocket.

    Ne contient aucune logique de Session : il se limite à la connexion,
    l'encodage/décodage, l'envoi, la répartition des messages reçus vers le
    listener, et la reconnexion avec backoff exponentiel après une coupure.
    Un message émis avant que le socket soit prêt est mis en attente et envoyé
    dès la connexion établie.
    """

    def __init__(
        self,
        client: ClientConfig,
        *,
        connect: Connector | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self._client = client
        self._connect = connect or websockets.connect
        self._sleep_fn = sleep or asyncio.sleep

        self._ws: ClientConnection | None = None
        self._client_id: str | None = None
        self._running = False
        self._connected = asyncio.Event()
        self._closed = asyncio.Event()
        self._recv_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._pending: list[ClientToServer] = []
        self._listener: TransportListener | None = None

    @property
    def client_id(self) -> str | None:
        return self._client_id

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._connected.is_set()

    def set_listener(self, listener: TransportListener) -> None:
        self._listener = listener

    async def run(self) -> None:
        """Maintient la connexion : connecte, et reconnecte avec backoff."""
        self._running = True
        delays = self._client.reconnect.delays()
        while self._running:
            try:
                await self.connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                delay = next(delays)
                log.warning(
                    "Connexion échouée, nouvel essai dans %.1fs: %s", delay, exc
                )
                await self._sleep(delay)
                continue

            delays = self._client.reconnect.delays()
            log.info("Connecté au serveur (client_id=%s)", self.client_id)
            await self._closed.wait()
            if not self._running:
                break

            delay = next(delays)
            log.warning("Connexion perdue, reconnexion dans %.1fs", delay)
            await self._sleep(delay)

        self._running = False

    async def connect(self) -> None:
        await self._cancel_task(self._recv_task)
        await self._cancel_task(self._ping_task)
        self._recv_task = None
        self._ping_task = None
        self._client_id = None
        self._closed.clear()

        self._ws = await self._connect(self._client.server_url)
        self._connected.set()

        await self._send(
            Register(
                client_tools=[],
                client_name=self._client.client_name,
                platform=self._client.platform,
            )
        )
        await self._flush_pending()

        self._recv_task = asyncio.create_task(self._receive_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def disconnect(self) -> None:
        self._running = False
        self._connected.clear()
        self._pending.clear()

        await self._cancel_task(self._ping_task)
        await self._cancel_task(self._recv_task)
        self._ping_task = None
        self._recv_task = None

        if self._ws:
            await self._ws.close()
            self._ws = None

        self._client_id = None
        self._closed.set()

    async def send_audio(self, data: bytes) -> None:
        await self._send(Audio(data=data))

    async def send_session_start(self) -> None:
        await self._send(SessionStart())

    async def send_interrupt(self) -> None:
        await self._send(Interrupt())

    async def _send(self, message: ClientToServer) -> None:
        ws = self._ws
        if ws is None or not self._connected.is_set():
            self._pending.append(message)
            return
        try:
            await ws.send(encode(message))
        except websockets.ConnectionClosed:
            self._connected.clear()

    async def _flush_pending(self) -> None:
        pending, self._pending = self._pending, []
        for message in pending:
            await self._send(message)

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if not isinstance(raw, bytes) or not raw:
                    continue
                try:
                    message = decode(raw)
                    await self._dispatch(message)
                except ProtocolError as exc:
                    log.warning("Message protocole invalide ignoré: %s", exc)
                    continue
                except Exception:
                    log.exception("Erreur de traitement d'un message ignorée")
                    continue
        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self._connected.clear()
            await self._cancel_task(self._ping_task)
            self._ping_task = None
            if self._running and self._listener:
                log.warning("Connexion au serveur perdue")
                try:
                    await self._listener.on_disconnect()
                except Exception:
                    log.exception("Erreur du handler de déconnexion ignorée")
            self._closed.set()

    async def _dispatch(self, message: ServerToClient) -> None:
        if isinstance(message, Registered):
            self._client_id = message.client_id
            return
        listener = self._listener
        if listener is None:
            return
        if isinstance(message, Audio):
            await listener.on_audio(message.data)
        elif isinstance(message, SessionStarted):
            await listener.on_session_started(message)
        elif isinstance(message, SessionEnded):
            await listener.on_session_ended(message)
        elif isinstance(message, Error):
            await listener.on_error(message)

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._client.ping_interval)
                if self.is_connected:
                    await self._send(Ping())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erreur de la boucle de ping ignorée")

    async def _sleep(self, delay: float) -> None:
        try:
            await self._sleep_fn(delay)
        except asyncio.CancelledError:
            self._running = False
            raise

    async def _cancel_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
