from __future__ import annotations

import asyncio
import logging

from protocol import Notification, SessionEnded, SessionStarted, Text

from client.audio_io import Audio
from client.config import AppConfig
from client.state import ClientState
from client.ws_client import Transport

log = logging.getLogger("athena.client")


class ClientApp:
    """Orchestrateur du Client desktop.

    Reçoit ses collaborateurs par injection (audio, transport) : rien n'est
    construit en dur ici, ce qui permet de tester le flux de Session avec des
    factices. Le client ne prend aucune décision de Session au-delà de
    ``LISTENING``/``ACTIVE`` : la fin de Session vient toujours du serveur.
    """

    def __init__(
        self,
        cfg: AppConfig,
        *,
        audio: Audio,
        transport: Transport,
    ) -> None:
        self._cfg = cfg
        self._audio = audio
        self._transport = transport

        self._state = ClientState.LISTENING
        self._playback: asyncio.Queue[bytes] = asyncio.Queue()
        self._stopped = asyncio.Event()

        transport.on_audio(self._handle_audio)
        transport.on_session_started(self._handle_session_started)
        transport.on_session_ended(self._handle_session_ended)
        transport.on_notification(self._handle_notification)
        transport.on_text(self._handle_text)
        transport.on_error(self._handle_error)
        transport.on_disconnect(self._handle_disconnect)

    @property
    def state(self) -> ClientState:
        return self._state

    async def enter(self) -> None:
        """Tap/Entrée : démarre une Session, ou interrompt en cours de Session."""
        if self._state is ClientState.ACTIVE:
            log.info("Barge-in : interruption et vidage du playback")
            await self._transport.send_interrupt()
            self._flush_playback()
            return
        log.info("Démarrage de Session demandé")
        await self._transport.send_session_start()

    async def run(self) -> None:
        self._stopped.clear()
        tasks = [asyncio.create_task(self._transport.run())]

        try:
            await self._audio.start_input()
            await self._audio.start_output()
            tasks.append(asyncio.create_task(self._capture_loop()))
            tasks.append(asyncio.create_task(self._playback_loop()))
            tasks.append(asyncio.create_task(self._stopped.wait()))
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._audio.close()
            await self._transport.disconnect()

    def stop(self) -> None:
        self._stopped.set()

    async def _capture_loop(self) -> None:
        while True:
            data = await self._audio.read_chunk()
            await self._forward_audio(data)

    async def _forward_audio(self, data: bytes) -> None:
        if not data:
            return
        if self._state is ClientState.ACTIVE:
            await self._transport.send_audio(data)

    async def _playback_loop(self) -> None:
        while True:
            data = await self._playback.get()
            await self._audio.write_chunk(data)

    def _flush_playback(self) -> None:
        while not self._playback.empty():
            self._playback.get_nowait()

    async def _handle_audio(self, data: bytes) -> None:
        await self._playback.put(data)

    async def _handle_session_started(self, msg: SessionStarted) -> None:
        self._state = ClientState.ACTIVE
        log.info("Session active")

    async def _handle_session_ended(self, msg: SessionEnded) -> None:
        self._state = ClientState.LISTENING
        self._flush_playback()
        log.info("Session terminée")

    async def _handle_disconnect(self) -> None:
        log.warning("Connexion perdue, retour en écoute")
        self._state = ClientState.LISTENING
        self._flush_playback()

    async def _handle_notification(self, msg: Notification) -> None:
        log.info("Notification de %s: %s", msg.source, msg.message)

    async def _handle_text(self, msg: Text) -> None:
        log.info("Texte [%s]: %s", msg.role, msg.content)

    async def _handle_error(self, message: str) -> None:
        log.error("Erreur serveur: %s", message)
