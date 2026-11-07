from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

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

    ``clock`` rend le temps injectable : le cooldown de la Veille est un
    échéancier comparé à cette horloge (``time.monotonic`` par défaut), ce qui
    permet de le tester sans attendre réellement. Le timeout de Session
    fantôme, lui, dort sur ``session_start_timeout_seconds`` ; les tests le
    configurent à une valeur très courte plutôt que d'attendre 10 s.
    """

    def __init__(
        self,
        cfg: AppConfig,
        *,
        audio: Audio,
        transport: Transport,
        detector: WakeWordDetector | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._cfg = cfg
        self._audio = audio
        self._transport = transport
        self._detector = detector
        # Une erreur d'inférence condamne la Veille pour le reste du processus :
        # le push-to-talk, lui, ne dépend pas du détecteur.
        self._detector_failed = False
        self._now = clock or time.monotonic

        self._state = ClientState.LISTENING
        self._playback: asyncio.Queue[bytes] = asyncio.Queue()
        self._stopped = asyncio.Event()
        self._cooldown_until = float("-inf")
        self._session_timeout_task: asyncio.Task[None] | None = None

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
        await self._begin_session()

    async def _begin_session(self) -> None:
        self._state = ClientState.ACTIVE
        await self._transport.send_session_start()
        self._start_session_timeout()

    def _start_session_timeout(self) -> None:
        """Arme le garde-fou contre une Session qui ne démarre jamais.

        Le déclenchement est optimiste : sans ``SessionStarted`` ni audio
        serveur, le Client reviendrait sinon rester bloqué en ``ACTIVE``.
        """
        self._cancel_session_timeout()
        delay = self._cfg.wake_word.session_start_timeout_seconds
        if delay <= 0:
            return
        self._session_timeout_task = asyncio.create_task(
            self._expire_phantom_session(delay),
            name="athena-client-session-timeout",
        )

    def _cancel_session_timeout(self) -> asyncio.Task[None] | None:
        """Annule le garde-fou et rend la tâche pour qu'elle soit attendue."""
        task, self._session_timeout_task = self._session_timeout_task, None
        if task is not None and not task.done():
            task.cancel()
        return task

    async def _expire_phantom_session(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if self._state is not ClientState.ACTIVE:
                return
            log.warning(
                "Session fantôme : aucune confirmation serveur (SessionStarted ou "
                "audio) après %.1f s, retour en écoute",
                delay,
            )
            await self._reset_to_listening()
        finally:
            if self._session_timeout_task is asyncio.current_task():
                self._session_timeout_task = None

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
            timeout_task = self._cancel_session_timeout()
            pending = list(tasks)
            if timeout_task is not None:
                pending.append(timeout_task)
            results = await asyncio.gather(*pending, return_exceptions=True)
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
        try:
            detector.close()
        except Exception:
            # Le démontage ne doit jamais échouer à cause du détecteur : le
            # micro et le transport sont déjà refermés à ce stade.
            log.exception("Échec de la fermeture du détecteur de Wake word")

    async def _capture_loop(self) -> None:
        while True:
            data = await self._audio.read_chunk()
            await self.forward_audio(data)

    def _wake_word_armed(self) -> bool:
        # La Veille coupée ne doit jamais consulter le détecteur, même si un
        # détecteur a été injecté (démarrage sans modèle, bascule de config).
        if (
            self._detector is None
            or self._detector_failed
            or not self._cfg.wake_word.enabled
        ):
            return False
        if self._state is not ClientState.LISTENING:
            return False
        # Le détecteur est suspendu tant qu'il reste de l'audio d'Athena à
        # jouer : sinon elle peut se déclencher elle-même.
        if not self._playback.empty():
            return False
        return self._now() >= self._cooldown_until

    def _detect_wake_word(self, data: bytes) -> Detection | None:
        detector = self._detector
        if detector is None or not self._wake_word_armed():
            return None
        try:
            return detector.process(data)
        except Exception:
            self._detector_failed = True
            log.exception(
                "Échec de l'inférence du Wake word : Veille désactivée pour le "
                "reste du processus, push-to-talk disponible"
            )
            return None

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
        await self._begin_session()
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
        cooldown = self._cfg.wake_word.cooldown_seconds
        self._cooldown_until = self._now() + max(0.0, cooldown)
        if cooldown > 0:
            log.info("Veille en cooldown pendant %.1f s", cooldown)

    async def _flush_playback(self) -> None:
        while not self._playback.empty():
            self._playback.get_nowait()
        await self._audio.flush()

    async def on_audio(self, data: bytes) -> None:
        # De l'audio serveur prouve que la Session est vivante : le garde-fou
        # contre la Session fantôme n'a plus lieu d'être.
        self._cancel_session_timeout()
        await self._playback.put(data)

    async def on_session_started(self, message: SessionStarted) -> None:
        self._cancel_session_timeout()
        self._state = ClientState.ACTIVE
        log.info("Session active")

    async def on_session_ended(self, message: SessionEnded) -> None:
        self._cancel_session_timeout()
        await self._reset_to_listening()
        log.info("Session terminée")

    async def on_disconnect(self) -> None:
        log.warning("Connexion perdue, retour en écoute")
        self._cancel_session_timeout()
        await self._reset_to_listening()

    async def on_error(self, message: Error) -> None:
        log.error("Erreur serveur: %s", message.message)
