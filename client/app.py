from __future__ import annotations

import asyncio
import logging

from protocol import Error, SessionEnded, SessionStarted

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

        transport.set_listener(self)

    @property
    def state(self) -> ClientState:
        return self._state

    async def tap(self) -> None:
        """Tap/Entrée : démarre une Session, ou interrompt en cours de Session."""
        if self._state is ClientState.ACTIVE:
            log.info("Barge-in : interruption et vidage du playback")
            await self._transport.send_interrupt()
            await self._flush_playback()
            return
        log.info("Démarrage de Session demandé")
        # Un tap démarre la Session côté client : la transmission du micro
        # commence tout de suite. Le serveur seul décide quand la Session
        # devient visible (il peut la refermer sans qu'Athena parle).
        self._state = ClientState.ACTIVE
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
            await self.forward_audio(data)

    async def forward_audio(self, data: bytes) -> None:
        if not data:
            return
        if self._state is ClientState.ACTIVE:
            await self._transport.send_audio(data)

    async def _playback_loop(self) -> None:
        while True:
            data = await self._playback.get()
            await self._audio.write_chunk(data)

    async def _reset_to_listening(self) -> None:
        self._state = ClientState.LISTENING
        await self._flush_playback()

    async def _flush_playback(self) -> None:
        while not self._playback.empty():
            self._playback.get_nowait()
        await self._audio.flush()

    async def on_audio(self, data: bytes) -> None:
        await self._playback.put(data)

    async def on_session_started(self, message: SessionStarted) -> None:
        self._state = ClientState.ACTIVE
        log.info("Session active")

    async def on_session_ended(self, message: SessionEnded) -> None:
        await self._reset_to_listening()
        log.info("Session terminée")

    async def on_disconnect(self) -> None:
        log.warning("Connexion perdue, retour en écoute")
        await self._reset_to_listening()

    async def on_error(self, message: Error) -> None:
        log.error("Erreur serveur: %s", message.message)
