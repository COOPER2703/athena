from __future__ import annotations

import asyncio
import logging

from protocol import Error, SessionEnded, SessionStarted

from client.audio_io import Audio
from client.config import AppConfig
from client.state import ClientState
from client.wake_word import Detection, WakeWordDetector
from client.ws_client import AccessTokenError, Transport

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
        detector: WakeWordDetector | None = None,
    ) -> None:
        self._cfg = cfg
        self._audio = audio
        self._transport = transport
        self._detector = detector

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
        # La tâche du transport est suivie à part : une erreur d'authentification
        # doit remonter jusqu'au point d'entrée au lieu d'être avalée par le
        # démontage.
        transport_task = asyncio.create_task(self._transport.run())
        tasks = [transport_task]

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
            results = await asyncio.gather(*tasks, return_exceptions=True)
            await self._audio.close()
            await self._transport.disconnect()
            self._close_detector()

        failure = results[0]
        if isinstance(failure, AccessTokenError):
            raise failure

    def stop(self) -> None:
        self._stopped.set()

    def _close_detector(self) -> None:
        detector = self._detector
        if detector is None:
            return
        detector.close()

    async def _capture_loop(self) -> None:
        while True:
            data = await self._audio.read_chunk()
            await self.forward_audio(data)

    def _detect_wake_word(self, data: bytes) -> Detection | None:
        # La Veille coupée ne doit jamais consulter le détecteur, même si un
        # détecteur a été injecté (démarrage sans modèle, bascule de config).
        if self._detector is None or not self._cfg.wake_word.enabled:
            return None
        return self._detector.process(data)

    async def forward_audio(self, data: bytes) -> None:
        if not data:
            return
        if self._state is ClientState.ACTIVE:
            await self._transport.send_audio(data)
            return
        await self._on_wake_word(data)

    async def _on_wake_word(self, data: bytes) -> None:
        detection = self._detect_wake_word(data)
        if detection is None:
            return
        if not self._transport.connected:
            # Pas de mise en file : une détection hors connexion ne doit pas
            # produire de Session fantôme à la reconnexion.
            log.warning(
                "Wake word détecté hors connexion, ignoré (score=%.3f)",
                detection.score,
            )
            return
        log.info("Wake word détecté (score=%.3f)", detection.score)
        await self._transport.send_session_start()
        self._state = ClientState.ACTIVE
        # Le pré-roll contient déjà le chunk qui a déclenché la détection :
        # il ne faut donc pas le renvoyer une seconde fois.
        if detection.pre_roll:
            await self._transport.send_audio(detection.pre_roll)

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
