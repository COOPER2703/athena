from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol

from server import __version__
from server.adapters.auth import SqliteAuthStore
from server.adapters.console import ConsoleAdapter
from server.adapters.console.settings import SettingsRoutes
from server.adapters.console.trace_buffer import TraceBuffer
from server.adapters.gemini import (
    GeminiEventJudge,
    GeminiKeyValidator,
    GeminiLiveAdapter,
    LiveConnect,
)
from server.adapters.memory import (
    GeminiFactExtractor,
    SentenceTransformerEmbedder,
    SqliteMemoryStore,
)
from server.adapters.plugins import PluginLoader
from server.adapters.settings import LayeredSecretResolver, SqliteSettingsStore
from server.adapters.trace import NullTraceSink, emit
from server.adapters.transport import ClientConnection, WebSocketTransport
from server.adapters.webhook import WebhookAdapter
from server.app.clock import AsyncioClock
from server.app.config import (
    AppConfig,
    config_with_stored_settings,
    default_env_file,
    loaded_settings,
    parse_dotenv,
)
from server.app.health import database_sizes
from server.app.secrets import SecretResolver
from server.app.thread import ThreadView
from server.app.trace import build_trace_sink, install_logging_bridge
from server.core.consolidation import (
    Consolidation,
    ConsolidationPolicy,
    ConsolidationReport,
    ConsolidationScheduler,
    local_now,
)
from server.core.commands import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    CloseLiveConnection,
    Command,
    FailTour,
    FinishTour,
    OpenLiveConnection,
    RefuseSession,
    RefuseTour,
    SendAudioToClient,
    SendAudioToLlm,
    SendAnnouncementToLlm,
    SendAudioToTour,
    SendMemoryToLlm,
    SendTextToTour,
    SendToolCallToTour,
    SendToolResultToLlm,
    SendUserTextToLlm,
    StartIdleTimer,
    Trace,
)
from server.core.coordinator import Coordinator
from server.core.events import (
    Event,
    EventReceived,
    ShutdownRequested,
    TourCanceled,
    TourRequested,
)
from server.core.health import HealthState, health_snapshot
from server.core.memory_tools import register_memory_tools
from server.core.plugins import PluginRegistry
from server.core.ports import (
    AuthService,
    Embedder,
    EventJudge,
    InstallationStatus,
    MemoryStore,
    MissingSecretError,
    SecretStore,
    SecretValidator,
    SettingsStore,
    TourControl,
    TourDestination,
    TraceEntry,
    TraceSink,
)
from server.core.session_tools import register_session_tools
from server.core.settings import GEMINI_SECRET, SettingsView
from server.core.tools import ToolRegistry

_TRACE_SOURCE = "app"
_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "system-prompt.txt"
_JUDGE_PROMPT_PATH = Path(__file__).resolve().parent.parent / "judge-prompt.txt"
_ANNOUNCE_PROMPT_PATH = Path(__file__).resolve().parent.parent / "announce-prompt.txt"


def load_system_prompt(path: str | Path | None = None) -> str:
    """Read the versioned generic system prompt injected into the kernel."""
    return Path(path or _SYSTEM_PROMPT_PATH).read_text(encoding="utf-8")


def load_judge_prompt(path: str | Path | None = None) -> str:
    """Read the versioned judgment prompt used by the text Event judge."""
    return Path(path or _JUDGE_PROMPT_PATH).read_text(encoding="utf-8")


def load_announce_prompt(path: str | Path | None = None) -> str:
    """Read the versioned announcement instruction of the Announce connection."""
    return Path(path or _ANNOUNCE_PROMPT_PATH).read_text(encoding="utf-8")


def server_tool_names(plugins: PluginRegistry) -> Callable[[], list[str]]:
    """Fournisseur des noms de Tools serveur (noyau + plugins).

    Le transport le rappelle à chaque ``Register`` : ``Registered.server_tools``
    reflète donc le registre au moment de la connexion, y compris les Tools d'un
    plugin activé par la retente après un démarrage dégradé.
    """

    def names() -> list[str]:
        return [spec.name for spec in plugins.tools.specs()]

    return names


def _trace_consolidation(
    trace_sink: TraceSink, report: ConsolidationReport
) -> None:
    """Émet la décision de Consolidation dans le flux unique (ADR-0004)."""
    emit(
        trace_sink,
        "consolidation",
        "Consolidation",
        payload={
            "facts_extracted": report.facts_extracted,
            "facts_stored": report.facts_stored,
            "duplicates": report.duplicates,
        },
    )
    emit(
        trace_sink,
        "consolidation",
        "ConsolidationDetail",
        payload={
            "since": report.since.isoformat() if report.since else None,
            "purged": report.purged,
            "transcript_chars": report.transcript_chars,
        },
        level="debug",
    )


def _record_consolidation(
    health: HealthState, trace_sink: TraceSink, report: ConsolidationReport
) -> None:
    """Trace la Consolidation **et** la mémorise pour le bandeau de santé.

    Le planificateur tourne en tâche de fond : la Console ne peut pas attendre
    un ``await`` sur ``memory.last_consolidation`` dans un instantané synchrone
    (ADR-0016). Le bilan est donc retenu ici, au passage du rapport.
    """
    _trace_consolidation(trace_sink, report)
    health.record_consolidation(report)


class WebhookServer(Protocol):
    """Couture de l'adaptateur Webhook : sert la surface HTTP et pousse dans le Canal.

    ``push`` est le ``App.push_event`` injecté à ``start`` : de la sorte la racine
    de composition n'a pas à connaître aiohttp, et l'adaptateur n'a aucune file
    propre (ADR-0010).
    """

    async def start(self, push: Callable[[str, str], None]) -> None: ...

    async def stop(self) -> None: ...


class Transport(Protocol):
    """Couture du transport audio, vue par la racine de composition.

    Enumère tout ce que ``App`` utilise : le cycle de vie, la file entrante,
    l'exécution des Commandes et les Clients branchés (lus par l'instantané de
    lecture de la Console).
    """

    @property
    def clients(self) -> Mapping[str, ClientConnection]: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def recv(self) -> Event: ...

    async def execute(self, command: Command) -> None: ...


class Clock(Protocol):
    """Couture de l'horloge d'inactivité (minuteurs de Session et de fenêtre).

    L'horloge de production est ``AsyncioClock`` ; les tests peuvent fournir
    une horloge virtuelle qui n'attend pas le temps réel.
    """

    def schedule(self, connection_id: str, timeout: float) -> None: ...

    def cancel(self, connection_id: str) -> None: ...

    async def recv(self) -> Event: ...

    def close(self) -> None: ...


class ConsoleServer(Protocol):
    """Couture de la Console web : instantané, Événement et Tours.

    ``snapshot`` est le ``App.snapshot`` injecté à ``start`` : l'adaptateur ne
    connaît ni le Coordinator ni ``SessionState``, et l'instantané est construit
    par la racine de composition au moment de la requête (ADR-0016). ``push``
    est le même ``App.push_event`` que celui du Webhook : l'Événement injecté
    entre dans le Canal d'événements unique, sans file propre à la Console.
    ``tour`` est la couture du **Tour** (ADR-0023) : la Console soumet et annule,
    elle n'atteint jamais le Coordinator.
    """

    async def start(
        self,
        snapshot: Callable[[], Mapping[str, object]],
        push: Callable[[str, str], None] | None = None,
        tour: TourControl | None = None,
    ) -> None: ...

    async def stop(self) -> None: ...


class _DeferredTraceSink:
    """Relais du flux de traces, branché après la configuration effective.

    Le magasin de réglages est construit avant le fan-out — il porte
    ``log_level`` — mais il ne peut recevoir son consommateur qu'une fois la
    configuration effective calculée. Ce relais garde cet ordre sans perdre
    d'entrée : la clé maître et la connexion SQLite du magasin sont paresseuses,
    donc rien n'est émis avant ``attach``.
    """

    def __init__(self) -> None:
        self._sink: TraceSink = NullTraceSink()

    def attach(self, sink: TraceSink) -> None:
        self._sink = sink

    def emit(self, entry: TraceEntry) -> None:
        self._sink.emit(entry)


class Installation:
    """État d'Installation d'une instance (ADR-0025), recalculé à la demande.

    ``setup_required`` appartient toujours au module d'auth : « aucun Compte ».
    L'Installation est plus large — elle se termine quand le Compte existe **et**
    qu'un Secret Gemini utilisable est disponible. La valeur effective suit la
    précédence de la résolution (environnement d'abord, puis magasin), et
    ``restart_required`` naît de l'écart entre cette valeur et celle chargée au
    démarrage : écrire une clé ne redémarre rien, mais la rend visible.
    """

    def __init__(
        self,
        *,
        auth: AuthService | None,
        secrets: SecretStore | None,
        runtime_key: str,
    ) -> None:
        self._auth = auth
        self._secrets = secrets
        self._runtime_key = runtime_key.strip()

    @property
    def gemini_configured(self) -> bool:
        """Vraie si une clé utilisable existe maintenant (runtime ou magasin)."""
        if self._runtime_key:
            return True
        if self._secrets is None:
            return False
        stored = self._secrets.secret(GEMINI_SECRET)
        return bool(stored and stored.strip())

    def status(self) -> InstallationStatus:
        account_exists = self._auth is None or not self._auth.setup_required
        configured = self.gemini_configured
        running = bool(self._runtime_key)
        return InstallationStatus(
            gemini_configured=configured,
            installation_complete=account_exists and configured,
            restart_required=configured != running,
            secrets_unreadable=bool(
                self._secrets is not None and self._secrets.unreadable
            ),
        )

    def describe(self) -> dict[str, object]:
        """Faits d'Installation de l'instantané, sans Secret (ticket #177).

        Complète ``status()`` de la seule chose qu'il ne porte pas : l'existence
        du Compte. ``restart_required`` reste la valeur calculée au même moment,
        pour que l'instantané et la Console ne divergent jamais.
        """
        status = self.status()
        return {
            "account_exists": self._auth is None or not self._auth.setup_required,
            "gemini_configured": status.gemini_configured,
            "complete": status.installation_complete,
            "secrets_unreadable": status.secrets_unreadable,
            "restart_required": status.restart_required,
        }


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
        transport: Transport | None = None,
        gemini: GeminiLiveAdapter | None = None,
        clock: Clock,
        trace_sink: TraceSink,
        memory: MemoryStore | None = None,
        user_id: str = "owner",
        system_prompt: str = "",
        announce_prompt: str = "",
        judge: EventJudge | None = None,
        tools: ToolRegistry | None = None,
        plugins: PluginRegistry | None = None,
        plugin_loader: PluginLoader | None = None,
        plugin_retry_interval: float = 5.0,
        recall_k: int = 5,
        recall_window_turns: int = 3,
        recall_max_distance: float = 0.6,
        scheduler: ConsolidationScheduler | None = None,
        webhook: WebhookServer | None = None,
        console: ConsoleServer | None = None,
        snapshot_config: Mapping[str, object] | None = None,
        tour_destination: TourDestination | None = None,
        installation: Callable[[], Mapping[str, object]] | None = None,
        health: HealthState | None = None,
        databases: Mapping[str, str | Path] | None = None,
        trace_buffer: Callable[[], Mapping[str, int]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        # Le transport et Gemini peuvent être **absents** : sans clé Gemini,
        # l'instance est en Installation et ces surfaces d'exécution restent
        # fermées (ADR-0025). Le noyau ne reçoit alors aucun Événement d'eux.
        self._transport = transport
        self._gemini = gemini
        self._clock = clock
        self._trace_sink = trace_sink
        self._memory = memory
        self._scheduler = scheduler
        self._webhook = webhook
        self._console = console
        # Destination des Tours (ADR-0023) : l'adaptateur Console l'implémente,
        # la racine la lui injecte ici. Absente, un Tour se déroule sans être
        # diffusé — jamais une erreur du noyau.
        self._tour_destination = tour_destination
        # Instantané non sensible de la configuration : la racine de
        # composition choisit explicitement les clés exposées, pour qu'aucun
        # secret ne puisse s'inviter dans l'instantané par accident.
        self._snapshot_config = dict(snapshot_config or {})
        # Diagnostic (#177) : l'Installation et la santé sont des lectures, pas
        # des ports du noyau. La racine injecte les faits ; ``snapshot()`` ne
        # fait que les assembler, sans ``await`` (ADR-0016).
        self._installation = installation
        self._health = health if health is not None else HealthState(now=time.time)
        self._databases = dict(databases or {})
        self._trace_buffer = trace_buffer
        # Uptime : horloge **monotone** injectable, jamais l'horloge de Session.
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._started_at: float | None = None
        self._plugins = plugins
        # Le registre des Tools est celui du Plugin Registry quand il existe :
        # les Tools du noyau y sont déjà enregistrés (propriétaire ``core``) et
        # les Tools de plugin s'y ajoutent au démarrage.
        self._tools = plugins.tools if plugins is not None else tools
        self._plugin_loader = plugin_loader
        # Cadence de la retente des plugins en échec, possédée par l'App : une
        # seule tâche de fond, jamais sur le chemin d'un Événement (ADR-0005).
        self._plugin_retry_interval = plugin_retry_interval
        self._coordinator = Coordinator(
            memory=memory,
            user_id=user_id,
            system_prompt=system_prompt,
            announce_prompt=announce_prompt,
            judge=judge,
            tools=self._tools,
            plugins=plugins,
            recall_k=recall_k,
            recall_window_turns=recall_window_turns,
            recall_max_distance=recall_max_distance,
        )
        self._events: asyncio.Queue[Event] = asyncio.Queue()

    @property
    def scheduler(self) -> ConsolidationScheduler | None:
        return self._scheduler

    @property
    def tools(self) -> ToolRegistry | None:
        return self._tools

    @property
    def plugins(self) -> PluginRegistry | None:
        return self._plugins

    @property
    def console(self) -> ConsoleServer | None:
        return self._console

    @property
    def uptime_seconds(self) -> float:
        """Secondes écoulées depuis le démarrage (0 avant ``run``)."""
        if self._started_at is None:
            return 0.0
        return max(0.0, self._monotonic() - self._started_at)

    def snapshot(self) -> Mapping[str, object]:
        """Instantané de lecture de l'état du noyau, prêt pour le JSON.

        Construit **à la demande** par la racine de composition, jamais poussé
        par le noyau : la Console lit un instantané de lecture et ne peut pas
        atteindre le Coordinator (ADR-0016). Aucun secret : ``config`` ne porte
        que les valeurs explicitement injectées par ``build_app``.

        L'appel est **synchrone** (le diffuseur d'état le fait toutes les
        secondes) : l'Installation et la santé sont des faits déjà mémorisés, et
        les tailles des bases se lisent par ``stat``, sans attendre.
        """
        clients = self._transport.clients if self._transport is not None else {}
        installation, restart_required = self._installation_block()
        return {
            "session": {
                "state": self._coordinator.state.value,
                "session_id": self._coordinator.session_id,
                "connection_id": self._coordinator.connection_id,
                "active_client_id": self._coordinator.owner_client_id,
            },
            "clients": [
                {
                    "client_id": client.client_id,
                    "client_name": client.client_name,
                    "platform": client.platform,
                }
                for client in sorted(
                    clients.values(), key=lambda item: item.client_name
                )
            ],
            "plugins": self._plugin_summaries(),
            "config": dict(self._snapshot_config),
            "installation": installation,
            "restart_required": restart_required,
            "health": health_snapshot(
                version=str(self._snapshot_config.get("version", "")),
                uptime_seconds=self.uptime_seconds,
                state=self._health,
                databases=database_sizes(self._databases),
                trace_buffer=(
                    self._trace_buffer() if self._trace_buffer is not None else None
                ),
            ),
        }

    def _installation_block(self) -> tuple[dict[str, object], bool]:
        """Section ``installation`` et drapeau de redémarrage, sans Secret.

        La liste des clés est fermée : même si — par accident — le fournisseur
        rendait un champ de plus, il ne passerait pas. ``surfaces`` décrit ce
        qui tourne réellement dans **ce** processus, ce qui rend visible une clé
        enregistrée mais pas encore appliquée.
        """
        raw = dict(self._installation()) if self._installation is not None else {}
        restart_required = bool(raw.get("restart_required", False))
        gemini_running = self._gemini is not None
        installation = {
            "account_exists": bool(raw.get("account_exists", True)),
            "gemini_configured": bool(
                raw.get("gemini_configured", gemini_running)
            ),
            "complete": bool(raw.get("complete", gemini_running)),
            "secrets_unreadable": bool(raw.get("secrets_unreadable", False)),
            "surfaces": {
                "gemini": gemini_running,
                "transport": self._transport is not None,
                "webhook": self._webhook is not None,
            },
        }
        return installation, restart_required

    def _plugin_summaries(self) -> list[Mapping[str, object]]:
        """Plugins connus, état de chargement et Tools attribués au propriétaire."""
        plugins = self._plugins
        if plugins is None:
            return []
        tools_by_owner: dict[str, list[str]] = {}
        for spec in plugins.tools.specs():
            owner = plugins.tools.owner_of(spec.name)
            if owner is not None:
                tools_by_owner.setdefault(owner, []).append(spec.name)
        return [
            {
                "name": name,
                "state": state,
                "tools": sorted(tools_by_owner.get(name, [])),
            }
            for name, state in sorted(plugins.states.items())
        ]

    async def run(self) -> None:
        """Démarre les adaptateurs puis traite les Événements jusqu'à l'arrêt.

        Les plugins sont chargés en premier : un défaut de plugin activé fait
        échouer le démarrage avant que le transport ne serve quoi que ce soit,
        et avant la trace ``started``.
        """
        # Départ de l'uptime : horloge monotone, prise avant tout service pour
        # que le bandeau de santé mesure la vie du processus, pas la Session.
        self._started_at = self._monotonic()
        await self._load_plugins()
        await self._warmup_memory()
        if self._transport is not None:
            await self._transport.start()
        if self._webhook is not None:
            await self._webhook.start(self.push_event)
        if self._console is not None:
            await self._console.start(self.snapshot, self.push_event, self)
        self._emit("started", payload={"state": self._coordinator.state.value})

        pumps = [asyncio.create_task(self._pump(self._clock.recv))]
        if self._transport is not None:
            pumps.append(asyncio.create_task(self._pump(self._transport.recv)))
        if self._gemini is not None:
            pumps.append(asyncio.create_task(self._pump(self._gemini.recv)))
        if self._scheduler is not None:
            # Background task: never on the event-handling path (ADR-0009).
            pumps.append(asyncio.create_task(self._scheduler.run()))
        if self._has_pending_plugins():
            # Une panne de plateforme se répare seule : la retente tourne en
            # tâche de fond et les Tools arrivent à la Connexion Live suivante.
            pumps.append(asyncio.create_task(self._retry_plugins()))
        try:
            await self._core_loop()
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            if self._transport is not None:
                await self._transport.stop()
            if self._webhook is not None:
                await self._webhook.stop()
            if self._console is not None:
                await self._console.stop()
            self._clock.close()
            self._emit("stopped")
            close = getattr(self._trace_sink, "close", None)
            if close is not None:
                close()

    async def _warmup_memory(self) -> None:
        warmup = getattr(self._memory, "warmup", None)
        if warmup is not None:
            await warmup()

    async def _load_plugins(self) -> None:
        """Charge les plugins activés dans le registre, avant tout service."""
        if self._plugin_loader is None or self._plugins is None:
            return
        await self._plugin_loader.load(self._plugins, push_event=self.push_event)

    def _has_pending_plugins(self) -> bool:
        loader = self._plugin_loader
        return loader is not None and bool(loader.pending)

    async def _retry_plugins(self) -> None:
        """Retente en tâche de fond les plugins dont ``create`` a échoué.

        La boucle appartient à l'App (ADR-0005) : elle attend l'intervalle, fait
        une tentative par plugin en échec, et s'arrête dès qu'il n'en reste
        aucun. Elle est annulée par le ``finally`` de ``run`` comme les pompes.
        """
        loader = self._plugin_loader
        registry = self._plugins
        if loader is None or registry is None:  # pragma: no cover - défensif
            return
        while loader.pending:
            await asyncio.sleep(self._plugin_retry_interval)
            await loader.retry_pending(registry, push_event=self.push_event)

    def request_shutdown(self) -> None:
        """Dépose ``ShutdownRequested`` dans le flux, appelable depuis un signal."""
        self._events.put_nowait(ShutdownRequested())

    def push_event(self, source: str, content: str) -> None:
        """Dépose un Événement dans le Canal d'événements unique du noyau.

        Point d'entrée noyau prêt pour un futur adaptateur Webhook (ADR-0007) :
        le noyau seul décide où et quand le juger.
        """
        self._events.put_nowait(EventReceived(source=source, content=content))

    def submit_tour(self, tour_id: str, text: str) -> None:
        """Dépose un Tour texte dans le Canal (couture ``TourControl``, ADR-0023).

        La Console ne connaît ni le Coordinator ni l'état de Session : elle
        demande, le noyau décide — y compris de refuser quand il est occupé.
        """
        self._events.put_nowait(TourRequested(tour_id=tour_id, text=text))

    def cancel_tour(self, tour_id: str) -> None:
        """Dépose l'annulation d'un Tour (flux SSE fermé par le navigateur)."""
        self._events.put_nowait(TourCanceled(tour_id=tour_id))

    async def _pump(self, recv: Callable[[], Awaitable[Event]]) -> None:
        while True:
            await self._events.put(await recv())

    async def _core_loop(self) -> None:
        while True:
            event = await self._events.get()
            for command in await self._coordinator.handle(event):
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
            command,
            (
                OpenLiveConnection,
                CloseLiveConnection,
                SendAudioToLlm,
                SendMemoryToLlm,
                SendUserTextToLlm,
                SendAnnouncementToLlm,
                SendToolResultToLlm,
            ),
        ):
            if self._gemini is not None:
                await self._gemini.execute(command)
            else:
                # Installation sans clé : aucune Connexion Live ne peut exister.
                # Le noyau n'en émet pas, mais une Commande tardive ne doit pas
                # faire tomber la boucle.
                self._emit(
                    "command_dropped",
                    payload={"command": type(command).__name__},
                    level="debug",
                )
            return
        if isinstance(
            command,
            (SendAudioToClient, AnnounceSessionStarted, AnnounceSessionEnded),
        ):
            if self._transport is not None:
                await self._transport.execute(command)
            return
        if isinstance(
            command,
            (
                SendTextToTour,
                SendAudioToTour,
                SendToolCallToTour,
                FinishTour,
                FailTour,
                RefuseTour,
            ),
        ):
            self._deliver_to_tour(command)
            return
        if isinstance(command, RefuseSession):
            # Refus lisible par le Client, sans changer le Protocole : le
            # transport le traduit en message ``Error`` existant.
            if self._transport is not None:
                await self._transport.execute(command)
            else:
                self._emit(
                    "command_dropped",
                    payload={"command": type(command).__name__},
                    level="debug",
                )
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

    def _deliver_to_tour(self, command: Command) -> None:
        """Route une Commande de Tour vers sa destination (port du noyau).

        Sans destination (Console fermée, test du noyau seul), la Commande est
        simplement abandonnée : le Tour s'est déroulé, personne ne l'écoutait.
        """
        destination = self._tour_destination
        if destination is None:
            self._emit(
                "command_dropped",
                payload={"command": type(command).__name__},
                level="debug",
            )
            return
        if isinstance(command, SendTextToTour):
            destination.text(command.tour_id, command.text)
            return
        if isinstance(command, SendAudioToTour):
            destination.audio(command.tour_id, command.data)
            return
        if isinstance(command, SendToolCallToTour):
            destination.tool(
                command.tour_id,
                command.name,
                command.arguments,
                command.status,
            )
            return
        if isinstance(command, FinishTour):
            destination.done(command.tour_id, command.reason)
            return
        if isinstance(command, FailTour):
            destination.failed(command.tour_id, command.code)
            return
        if isinstance(command, RefuseTour):
            destination.busy(command.tour_id)


def build_app(
    config: AppConfig,
    *,
    secrets: SecretResolver | None = None,
    settings: SecretStore | None = None,
    secret_validator: SecretValidator | None = None,
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
    live_connect: LiveConnect | None = None,
    clock: Clock | None = None,
    embedder: Embedder | None = None,
    auth_now: Callable[[], float] | None = None,
    monotonic: Callable[[], float] | None = None,
    restart: Callable[[], None] | None = None,
) -> App:
    """Construit explicitement : config → trace → adaptateurs → Coordinator.

    ``secrets`` résout les secrets des plugins et la clé Gemini ; par défaut un
    résolveur à trois couches (environnement du processus > magasin > ``.env``)
    applique la précédence de l'ADR-0024. ``settings`` remplace le magasin
    construit depuis ``config.settings`` (seam de test). ``secret_validator``
    remplace la validation réelle d'une clé Gemini.

    Sans clé Gemini utilisable, l'instance démarre en **Installation** : le
    transport, le Webhook et Gemini ne sont pas construits, la Console et ses
    routes de Réglages et de Secrets restent servies (ADR-0025).

    ``live_connect``, ``clock``, ``embedder``, ``auth_now``, ``monotonic``,
    ``settings`` et ``secret_validator`` sont les coutures laissées ouvertes au
    harnais de test serveur : le serveur garde tous ses autres adaptateurs réels
    (Console, auth, mémoire SQLite), et la configuration effective se remplace
    sans relancer le processus. ``restart`` remplace la sortie propre demandée
    par l'application des Réglages : le harnais observe le redémarrage sans
    tuer pytest.
    """
    # Magasin de réglages et de Secrets (ADR-0024) : base dédiée à côté de la
    # mémoire et de l'auth. Il précède le flux de traces — il porte
    # ``log_level`` — et reçoit un relais dont la cible n'est connue qu'après
    # l'application du magasin.
    settings_sink = _DeferredTraceSink()
    settings_store: SecretStore = (
        settings
        if settings is not None
        else SqliteSettingsStore(
            db_path=config.settings.db_path,
            key_path=config.settings.master_key_file,
            trace_sink=settings_sink,
        )
    )

    # Précédence des Secrets : environnement du processus > magasin > .env.
    # Le fichier reste une couche distincte, sinon un Secret du magasin serait
    # masqué par une vieille valeur du fichier.
    process_env = os.environ if env is None else env
    dotenv = parse_dotenv(
        env_file if env_file is not None else default_env_file()
    )
    secret_resolver: SecretResolver = (
        secrets
        if secrets is not None
        else LayeredSecretResolver(
            process_env=process_env, store=settings_store, dotenv=dotenv
        )
    )
    api_key = (
        _optional_secret(secret_resolver, GEMINI_SECRET)
        or config.gemini.api_key
    ).strip()
    gemini_enabled = bool(api_key)
    config = config.model_copy(
        update={"gemini": config.gemini.model_copy(update={"api_key": api_key})}
    )
    # La couche « magasin » s'applique **avant** le premier adaptateur : une
    # valeur enregistrée prend effet au démarrage, sinon le GET annoncerait une
    # valeur que le processus n'utilise pas (ADR-0024).
    config, applied_settings = config_with_stored_settings(
        config, store=_settings_values(settings_store), process_env=process_env
    )

    console_trace_buffer = (
        TraceBuffer(config.console.trace_buffer) if config.console.enabled else None
    )
    # Mémoire de santé (#177) : elle écoute le même flux et retient la dernière
    # erreur, sans ouvrir de second canal. Construite avant le fan-out, qui la
    # reçoit comme sink supplémentaire.
    health = HealthState(now=time.time)
    extra_sinks: list[TraceSink] = [health]
    if console_trace_buffer is not None:
        extra_sinks.append(console_trace_buffer)
    trace_sink = build_trace_sink(
        debug_decisions=config.debug_decisions,
        log_dir=config.log_dir,
        level=config.log_level,
        extra_sinks=extra_sinks,
    )
    # Le relais rejoint le fan-out : plus aucune entrée du magasin n'est perdue.
    settings_sink.attach(trace_sink)
    install_logging_bridge(
        trace_sink,
        level="DEBUG" if config.debug_decisions else config.log_level,
    )

    # Authentification applicative : base dédiée, comme la mémoire, et jamais
    # de secret par défaut. Les surfaces reçoivent ce service par leur
    # constructeur : Console, transport et Webhook partagent le même store,
    # donc la révocation vaut partout sans redémarrage (ADR-0017).
    auth = SqliteAuthStore(
        db_path=config.auth.db_path,
        trace_sink=trace_sink,
        now=auth_now if auth_now is not None else time.time,
        ticket_ttl_seconds=config.auth.ticket_days * 24 * 60 * 60,
        ticket_refresh_seconds=config.auth.ticket_refresh_seconds,
        password_min_length=config.auth.password_min_length,
        login_max_attempts=config.auth.login_max_attempts,
        login_block_seconds=config.auth.login_block_minutes * 60,
    )

    # Tools du noyau et Tools de plugin partagent un seul registre : celui du
    # Plugin Registry. Les Tools du noyau y entrent au propriétaire ``core``,
    # les plugins activés s'y ajoutent au démarrage. Le registre est construit
    # avant le transport pour que ``Registered.server_tools`` le lise à chaque
    # enregistrement, plugin chargé ou non.
    plugins = PluginRegistry()

    transport: WebSocketTransport | None = None
    gemini: GeminiLiveAdapter | None = None
    if gemini_enabled:
        transport = WebSocketTransport(
            host=config.server.host,
            port=config.server.port,
            server_tools=server_tool_names(plugins),
            trace_sink=trace_sink,
            # Même store que la Console : une révocation prononcée depuis la
            # surface d'observation vaut pour le transport, sans redémarrage.
            auth=auth,
        )
        gemini = GeminiLiveAdapter(
            api_key=api_key,
            model=config.gemini.model,
            voice=config.gemini.voice,
            live_connect=live_connect,
            trace_sink=trace_sink,
        )
    clock = clock if clock is not None else AsyncioClock()

    embedder = (
        embedder
        if embedder is not None
        else SentenceTransformerEmbedder(config.memory.embed_model)
    )
    memory = SqliteMemoryStore(
        db_path=config.memory.db_path,
        embedder=embedder,
        short_term_window=config.memory.short_term_window,
        recent_events_window=config.memory.recent_events_window,
        journal_retention_days=config.memory.journal_retention_days,
    )

    judge: GeminiEventJudge | None = None
    scheduler: ConsolidationScheduler | None = None
    if gemini_enabled:
        extractor = GeminiFactExtractor(
            api_key=api_key,
            model=config.memory.consolidation_model,
            trace_sink=trace_sink,
        )
        judge = GeminiEventJudge(
            api_key=api_key,
            model=config.gemini.judge_model,
            prompt=load_judge_prompt(),
            trace_sink=trace_sink,
        )
        policy = ConsolidationPolicy(config.memory.consolidation_hours)
        consolidation = Consolidation(
            memory=memory,
            extractor=extractor,
            policy=policy,
            user_id=config.memory.user_id,
            dedup_threshold=config.memory.dedup_threshold,
            journal_retention_days=config.memory.journal_retention_days,
        )
        scheduler = ConsolidationScheduler(
            consolidation=consolidation,
            policy=policy,
            now=local_now,
            sleep=asyncio.sleep,
            on_run=lambda report: _record_consolidation(health, trace_sink, report),
        )

    # Les Tools du noyau entrent au propriétaire ``core`` ; les plugins activés
    # s'y ajoutent au démarrage.
    register_memory_tools(
        plugins.tools,
        memory,
        config.memory.user_id,
        forget_max_distance=config.memory.recall_max_distance,
    )
    # Tool du noyau de fin de Session (#130) : enregistré avec les Tools du
    # noyau, donc avant les plugins. Une collision de nom reste fatale (ADR-0014).
    register_session_tools(plugins.tools)
    plugin_loader = PluginLoader(
        directory=config.plugins.directory,
        state_path=config.plugins.state_path,
        secrets=secret_resolver,
        trace_sink=trace_sink,
    )

    webhook: WebhookAdapter | None = None
    if config.webhook.enabled and gemini_enabled:
        webhook = WebhookAdapter(
            host=config.webhook.host,
            port=config.webhook.port,
            trace_sink=trace_sink,
            auth=auth,
        )

    # État d'Installation : Compte + Secret Gemini utilisable (ADR-0025).
    installation = Installation(auth=auth, secrets=settings_store, runtime_key=api_key)
    validator = (
        secret_validator
        if secret_validator is not None
        else GeminiKeyValidator(model=config.gemini.judge_model)
    )
    ensure_master_key = getattr(settings_store, "ensure_master_key", None)

    # Réglages : la Console reçoit les couches **figées** du démarrage, pas une
    # relecture de l'environnement. Le redémarrage est une callable : l'App
    # n'existe pas encore, la cellule la referme après construction. Une
    # callable injectée remplace la sortie réelle en test, pour ne pas tuer
    # pytest — c'est la seule couture de l'application.
    restart_cell: list[Callable[[], None]] = []

    def request_restart() -> None:
        if restart is not None:
            restart()
        elif restart_cell:
            restart_cell[0]()

    settings_routes = SettingsRoutes(
        view=SettingsView(
            loaded=loaded_settings(config),
            applied_store=applied_settings,
            process_env=dict(process_env),
            dotenv=dict(dotenv),
        ),
        store=_settings_store(settings_store),
        secrets=settings_store,
        # La présence de la callable est le drapeau : pas de second état.
        restart=request_restart if config.restart_on_apply else None,
        restart_command=config.restart_command,
    )

    console: ConsoleAdapter | None = None
    if config.console.enabled:
        console = ConsoleAdapter(
            host=config.console.host,
            port=config.console.port,
            static_dir=config.console.static_dir,
            trace_buffer=console_trace_buffer,
            trace_sink=trace_sink,
            auth=auth,
            secrets=settings_store,
            secret_validator=validator,
            settings=settings_routes,
            installation=installation.status,
            master_key=ensure_master_key,
            # Fil de la Conversation : lecture paginée du Journal, bornée à la
            # rétention de la mémoire courte (ADR-0023).
            thread=ThreadView(
                memory=memory,
                user_id=config.memory.user_id,
                retention_days=config.memory.journal_retention_days,
            ),
        )

    app = App(
        transport=transport,
        gemini=gemini,
        clock=clock,
        trace_sink=trace_sink,
        memory=memory,
        user_id=config.memory.user_id,
        system_prompt=load_system_prompt(),
        announce_prompt=load_announce_prompt(),
        judge=judge,
        tools=plugins.tools,
        plugins=plugins,
        plugin_loader=plugin_loader,
        recall_k=config.memory.recall_k,
        recall_window_turns=config.memory.recall_window_turns,
        recall_max_distance=config.memory.recall_max_distance,
        scheduler=scheduler,
        webhook=webhook,
        console=console,
        tour_destination=console.tour_destination if console is not None else None,
        snapshot_config={
            "model": config.gemini.model,
            "voice": config.gemini.voice,
            "user_id": config.memory.user_id,
            "memory_db": config.memory.db_path,
            "log_dir": config.log_dir,
            "version": __version__,
        },
        # Diagnostic (#177) : l'Installation et la santé sont recomposées à
        # chaque lecture. Les bases sont nommées ici, une fois pour toutes, dans
        # l'ordre du bandeau.
        installation=installation.describe,
        health=health,
        databases={
            "memory": config.memory.db_path,
            "auth": config.auth.db_path,
            "settings": config.settings.db_path,
        },
        trace_buffer=(
            console_trace_buffer.stats
            if console_trace_buffer is not None
            else None
        ),
        monotonic=monotonic,
    )
    # La cellule est remplie une fois l'App construite : un PUT qui demande le
    # redémarrage dépose ``ShutdownRequested`` dans la boucle unique.
    restart_cell.append(app.request_shutdown)
    return app


def _settings_values(store: SecretStore) -> dict[str, str]:
    """Valeurs du magasin, quand l'objet injecté en est un."""
    reader = getattr(store, "values", None)
    return dict(reader()) if callable(reader) else {}


def _settings_store(store: SecretStore) -> SettingsStore | None:
    """Le magasin de réglages, ou ``None`` si l'objet n'en est pas un.

    Réglages et Secrets partagent une base en production ; un test peut
    injecter un faux qui ne porte que les Secrets. Les routes répondent alors
    ``503 settings_unavailable`` plutôt que d'inventer un magasin.
    """
    required = ("value", "values", "set_value", "delete_value")
    return store if all(hasattr(store, name) for name in required) else None


def _optional_secret(resolver: SecretResolver, name: str) -> str | None:
    """Résout un Secret facultatif : son absence n'est pas une erreur ici."""
    try:
        return resolver.resolve(name)
    except MissingSecretError:
        return None
