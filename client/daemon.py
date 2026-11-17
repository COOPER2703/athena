"""Daemon du Client desktop : page locale + client audio.

Le daemon possède deux vies indépendantes :

- la **page locale de couplage** (``client.page``), servie dès le démarrage en
  boucle locale, qui affiche l'état et accepte une URL, un Jeton et une demande
  de reconnexion ;
- le **Client audio** proprement dit, reconstruit à chaque couplage ou
  reconnexion, qui parle le Protocole partagé au serveur Athena.

Cette séparation est ce qui rend la page utile : un serveur injoignable, un
Jeton refusé ou un micro absent laissent la page en vie avec une explication.
Un Jeton refusé ne fait donc plus sortir le processus — le daemon attend un
nouveau couplage ou un clic sur « Reconnecter ».
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import Callable, Mapping

from client.app import ClientApp
from client.audio_io import Audio, AudioIO
from client.config import AppConfig, ClientConfig
from client.page import CouplingPage
from client.settings import Coupling, SettingsStore, resolve_coupling
from client.wake_word import (
    OpenWakeWordDetector,
    WakeWordDetector,
    WakeWordUnavailableError,
)
from client.ws_client import (
    AccessTokenError,
    Transport,
    TransportObserver,
    WsTransport,
)

log = logging.getLogger("athena.client")

UNCONFIGURED_MESSAGE = (
    "Aucun Jeton d'accès enregistré : saisissez-en un pour coupler ce poste."
)


@dataclass(slots=True)
class RuntimeStatus:
    """État vivant affiché par la page ; jamais de secret ici."""

    connection: str = "idle"
    connection_message: str = ""
    mic: str = "unknown"
    mic_message: str = ""
    wake_word: str = "unknown"
    wake_word_message: str = ""
    client_state: str = "stopped"
    client_id: str | None = None


class _DaemonObserver:
    """Traduit les transitions du transport en état de page."""

    def __init__(self, daemon: "ClientDaemon") -> None:
        self._daemon = daemon

    def on_connecting(self) -> None:
        self._daemon._on_connecting()

    def on_connected(self) -> None:
        self._daemon._on_connected()

    def on_connect_failed(self, reason: str) -> None:
        self._daemon._on_connect_failed(reason)

    def on_disconnected(self) -> None:
        self._daemon._on_disconnected()


class ClientDaemon:
    """Supervise la page locale et le Client, sans jamais exposer le Jeton."""

    def __init__(
        self,
        cfg: AppConfig,
        store: SettingsStore,
        *,
        environ: Mapping[str, str] | None = None,
        audio_factory: Callable[[], Audio] | None = None,
        detector_factory: Callable[[], WakeWordDetector | None] | None = None,
        transport_factory: Callable[
            [ClientConfig, TransportObserver], Transport
        ]
        | None = None,
        page: CouplingPage | None = None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._environ = environ
        self._audio_factory = audio_factory or (lambda: AudioIO(cfg.audio))
        self._detector_factory = detector_factory or (
            lambda: OpenWakeWordDetector.from_config(cfg.wake_word)
        )
        self._transport_factory = transport_factory or (
            lambda client, observer: WsTransport(client, observer=observer)
        )
        self._page = (
            page
            if page is not None
            else CouplingPage(self, host=cfg.page.host, port=cfg.page.port)
        )
        self._status = RuntimeStatus()
        self._app: ClientApp | None = None
        self._worker: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def page(self) -> CouplingPage:
        return self._page

    @property
    def status(self) -> RuntimeStatus:
        return self._status

    def coupling(self) -> Coupling:
        return resolve_coupling(self._store.load(), self._environ)

    async def start(self) -> None:
        """Sert la page avant même de tenter la moindre connexion."""
        await self._page.start()
        log.info(
            "Page de couplage : http://%s:%d", self._page.host, self._page.port
        )
        await self.reconnect()

    async def stop(self) -> None:
        await self._stop_worker()
        await self._page.stop()

    async def reconnect(self) -> Mapping[str, object]:
        """Reconstruit le Client avec le couplage effectif courant."""
        async with self._lock:
            await self._stop_worker()
            self._reset_status()
            self._worker = asyncio.create_task(
                self._run_client(), name="athena-client-daemon"
            )
        return self.coupling_state()

    async def apply_coupling(
        self, *, server_url: str, access_token: str | None
    ) -> Mapping[str, object]:
        """Enregistre le couplage, puis reconnecte sans redémarrer le daemon."""
        try:
            self._store.save(server_url=server_url, access_token=access_token)
        except OSError:
            log.exception(
                "Impossible d'enregistrer le couplage dans %s", self._store.path
            )
            raise
        log.info("Couplage enregistré pour %s", server_url)
        return await self.reconnect()

    def coupling_state(self) -> Mapping[str, object]:
        """État public de la page : le Jeton n'y figure jamais."""
        coupling = self.coupling()
        status = self._status
        app = self._app
        wake_word = status.wake_word
        wake_word_message = status.wake_word_message
        if app is not None and app.detector_failed and wake_word != "unavailable":
            wake_word = "unavailable"
            wake_word_message = (
                "La Veille s'est arrêtée après une erreur d'inférence ; "
                "le push-to-talk reste disponible."
            )
        return {
            "server_url": coupling.server_url,
            "server_url_source": coupling.url_source,
            "token_configured": coupling.token_configured,
            "token_source": coupling.token_source,
            "connection": status.connection,
            "connection_message": self._scrub(status.connection_message),
            "mic": status.mic,
            "mic_message": self._scrub(status.mic_message),
            "wake_word": wake_word,
            "wake_word_message": self._scrub(wake_word_message),
            "client_state": (
                app.state.value if app is not None else status.client_state
            ),
            "client_id": app.client_id if app is not None else status.client_id,
        }

    async def tap(self) -> None:
        """Push-to-talk : la page ne capte pas le micro, stdin le fait."""
        app = self._app
        if app is None:
            log.info("Push-to-talk ignoré : aucune connexion active")
            return
        await app.tap()

    def _reset_status(self) -> None:
        coupling = self.coupling()
        status = self._status
        status.mic = "unknown"
        status.mic_message = ""
        status.client_state = "stopped"
        status.client_id = None
        if self._cfg.wake_word.enabled:
            status.wake_word = "unknown"
            status.wake_word_message = ""
        else:
            status.wake_word = "disabled"
            status.wake_word_message = "Veille désactivée (ATHENA_WAKE_WORD=0)."
        if coupling.token_configured:
            status.connection = "connecting"
            status.connection_message = "Connexion au serveur…"
        else:
            status.connection = "unconfigured"
            status.connection_message = UNCONFIGURED_MESSAGE

    async def _run_client(self) -> None:
        coupling = self.coupling()
        if not coupling.token_configured:
            return
        self._status.connection = "connecting"
        self._status.connection_message = "Connexion au serveur…"
        app: ClientApp | None = None
        try:
            detector = self._build_detector()
            audio = self._audio_factory()
            client_cfg = replace(
                self._cfg.client,
                server_url=coupling.server_url,
                access_token=coupling.access_token,
            )
            transport = self._transport_factory(
                client_cfg, _DaemonObserver(self)
            )
            app = ClientApp(
                self._cfg,
                audio=audio,
                transport=transport,
                detector=detector,
                on_ready=self._on_microphone_ready,
            )
            self._app = app
            await app.run()
        except AccessTokenError as exc:
            self._status.connection = "refused"
            self._status.connection_message = self._scrub(str(exc))
            log.warning("Jeton d'accès refusé : %s", self._scrub(str(exc)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._status.mic = "unavailable"
            self._status.mic_message = (
                self._scrub(str(exc)) or "Micro indisponible."
            )
            self._status.connection = "error"
            self._status.connection_message = (
                "Le Client s'est arrêté : " + self._status.mic_message
            )
            log.exception("Client arrêté par une erreur locale")
        finally:
            if app is not None and self._app is app:
                self._app = None
            if self._status.mic != "unavailable":
                self._status.mic = "unknown"
                self._status.mic_message = "Micro fermé."
            if self._status.wake_word == "ready":
                self._status.wake_word = "unknown"
                self._status.wake_word_message = "Veille arrêtée."
            self._status.client_state = "stopped"
            self._status.client_id = None

    async def _stop_worker(self) -> None:
        app, worker = self._app, self._worker
        self._app = None
        self._worker = None
        if app is not None:
            app.stop()
        if worker is None:
            return
        if worker is not asyncio.current_task() and not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    def _build_detector(self) -> WakeWordDetector | None:
        """Charge la Veille, ou l'annonce indisponible sans arrêter le daemon."""
        if not self._cfg.wake_word.enabled:
            self._status.wake_word = "disabled"
            self._status.wake_word_message = "Veille désactivée (ATHENA_WAKE_WORD=0)."
            return None
        self._status.wake_word = "loading"
        self._status.wake_word_message = "Chargement du modèle de Wake word…"
        try:
            detector = self._detector_factory()
        except WakeWordUnavailableError as exc:
            self._status.wake_word = "unavailable"
            self._status.wake_word_message = self._scrub(str(exc))
            log.warning("Veille indisponible : %s", self._scrub(str(exc)))
            return None
        except Exception as exc:
            # Un détecteur cassé ne doit jamais condamner le micro ni la page :
            # la Veille est signalée indisponible et le push-to-talk survit.
            self._status.wake_word = "unavailable"
            self._status.wake_word_message = self._scrub(
                f"Impossible de charger la Veille : {exc}"
            )
            log.exception("Échec inattendu du détecteur de Wake word")
            return None
        self._status.wake_word = "ready"
        self._status.wake_word_message = "Veille active."
        return detector

    def _on_microphone_ready(self) -> None:
        self._status.mic = "ready"
        self._status.mic_message = "Micro ouvert."

    def _on_connecting(self) -> None:
        self._status.connection = "connecting"
        self._status.connection_message = "Connexion au serveur…"

    def _on_connected(self) -> None:
        self._status.connection = "connected"
        self._status.connection_message = "Connecté au serveur."
        app = self._app
        self._status.client_id = app.client_id if app is not None else None

    def _on_connect_failed(self, reason: str) -> None:
        self._status.connection = "error"
        self._status.connection_message = (
            "Serveur injoignable : " + self._scrub(reason)
        )

    def _on_disconnected(self) -> None:
        self._status.connection = "disconnected"
        self._status.connection_message = (
            "Connexion perdue ; nouvelle tentative automatique."
        )

    def _scrub(self, message: str) -> str:
        """Retire le Jeton effectif d'un message avant de l'afficher."""
        token = self.coupling().access_token
        if token and token in message:
            return message.replace(token, "…")
        return message
