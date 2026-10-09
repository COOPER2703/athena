from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Protocol

import websockets
from websockets.asyncio.client import ClientConnection

from protocol import (
    Audio,
    Error,
    Interrupt,
    Notification,
    Ping,
    ProtocolError,
    Register,
    Registered,
    SessionEnded,
    SessionStart,
    SessionStarted,
    Text,
    decode,
    encode,
)

from client.config import BackoffConfig

log = logging.getLogger("athena.client")

AudioHandler = Callable[[bytes], Awaitable[None]]
SessionStartedHandler = Callable[[SessionStarted], Awaitable[None]]
SessionEndedHandler = Callable[[SessionEnded], Awaitable[None]]
NotificationHandler = Callable[[Notification], Awaitable[None]]
TextHandler = Callable[[Text], Awaitable[None]]
ErrorHandler = Callable[[str], Awaitable[None]]
DisconnectHandler = Callable[[], Awaitable[None]]


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

    def on_audio(self, handler: AudioHandler) -> None: ...

    def on_session_started(self, handler: SessionStartedHandler) -> None: ...

    def on_session_ended(self, handler: SessionEndedHandler) -> None: ...

    def on_notification(self, handler: NotificationHandler) -> None: ...

    def on_text(self, handler: TextHandler) -> None: ...

    def on_error(self, handler: ErrorHandler) -> None: ...

    def on_disconnect(self, handler: DisconnectHandler) -> None: ...


class WsTransport:
    """Transport WebSocket.

    Ne contient aucune logique de Session : il se limite à la connexion,
    l'encodage/décodage, l'envoi, la répartition des messages reçus vers les
    callbacks, et la reconnexion avec backoff exponentiel après une coupure.
    """

    def __init__(
        self,
        server_url: str,
        client_name: str = "desktop",
        platform: str = "desktop",
        ping_interval: float = 30.0,
        reconnect: BackoffConfig | None = None,
    ) -> None:
        self._server_url = server_url
        self._client_name = client_name
        self._platform = platform
        self._ping_interval = ping_interval
        self._reconnect = reconnect or BackoffConfig()

        self._ws: ClientConnection | None = None
        self._client_id: str | None = None
        self._running = False
        self._connected = asyncio.Event()
        self._closed = asyncio.Event()
        self._recv_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None

        self._on_audio: AudioHandler | None = None
        self._on_session_started: SessionStartedHandler | None = None
        self._on_session_ended: SessionEndedHandler | None = None
        self._on_notification: NotificationHandler | None = None
        self._on_text: TextHandler | None = None
        self._on_error: ErrorHandler | None = None
        self._on_disconnect: DisconnectHandler | None = None

    @property
    def client_id(self) -> str | None:
        return self._client_id

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._connected.is_set()

    def on_audio(self, handler: AudioHandler) -> None:
        self._on_audio = handler

    def on_session_started(self, handler: SessionStartedHandler) -> None:
        self._on_session_started = handler

    def on_session_ended(self, handler: SessionEndedHandler) -> None:
        self._on_session_ended = handler

    def on_notification(self, handler: NotificationHandler) -> None:
        self._on_notification = handler

    def on_text(self, handler: TextHandler) -> None:
        self._on_text = handler

    def on_error(self, handler: ErrorHandler) -> None:
        self._on_error = handler

    def on_disconnect(self, handler: DisconnectHandler) -> None:
        self._on_disconnect = handler

    async def run(self) -> None:
        """Maintient la connexion : connecte, et reconnecte avec backoff."""
        self._running = True
        delays = self._reconnect.delays()
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

            delays = self._reconnect.delays()
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

        self._ws = await websockets.connect(self._server_url)
        self._connected.set()

        await self._send(
            Register(
                client_tools=[],
                client_name=self._client_name,
                platform=self._platform,
            )
        )

        self._recv_task = asyncio.create_task(self._receive_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

    async def disconnect(self) -> None:
        self._running = False
        self._connected.clear()

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

    async def _send(self, message: object) -> None:
        if not self._ws:
            raise RuntimeError("Not connected")
        try:
            await self._ws.send(encode(message))
        except websockets.ConnectionClosed:
            self._connected.clear()

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if not isinstance(raw, bytes) or not raw:
                    continue
                try:
                    message = decode(raw)
                except ProtocolError as exc:
                    log.warning("Message protocole invalide ignoré: %s", exc)
                    continue
                await self._dispatch(message)
        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self._connected.clear()
            await self._cancel_task(self._ping_task)
            self._ping_task = None
            if self._running and self._on_disconnect:
                log.warning("Connexion au serveur perdue")
                await self._on_disconnect()
            self._closed.set()

    async def _dispatch(self, message: object) -> None:
        if isinstance(message, Registered):
            self._client_id = message.client_id
        elif isinstance(message, Audio):
            if self._on_audio:
                await self._on_audio(message.data)
        elif isinstance(message, SessionStarted):
            if self._on_session_started:
                await self._on_session_started(message)
        elif isinstance(message, SessionEnded):
            if self._on_session_ended:
                await self._on_session_ended(message)
        elif isinstance(message, Notification):
            if self._on_notification:
                await self._on_notification(message)
        elif isinstance(message, Text):
            if self._on_text:
                await self._on_text(message)
        elif isinstance(message, Error):
            if self._on_error:
                await self._on_error(message.message)

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                if self.is_connected:
                    await self._send(Ping())
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _sleep(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
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
