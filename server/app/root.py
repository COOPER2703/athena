from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo

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
from server.adapters.plugins.admin import PluginAdmin
from server.adapters.plugins.catalogue import PluginCatalogue
from server.adapters.plugins.state import PluginStateStore
from server.adapters.schedule import SqliteScheduleStore
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
from server.app.schedule_wakeup import AsyncioWakeup
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
from server.core.coordinator import NOW_CONTEXT_HEADER, Coordinator
from server.core.events import (
    ClientAudio,
    ClientDisconnected,
    ClientRegistered,
    Event,
    EventReceived,
    LlmOpened,
    LlmTranscription,
    OccurrenceReceived,
    SessionRequested,
    ShutdownRequested,
    TourCanceled,
    TourRequested,
)
from server.core.health import HealthState, health_snapshot
from server.core.memory_tools import register_memory_tools
from server.core.planificateur import Planificateur
from server.core.plugins import PluginRegistry
from server.core.ports import (
    AuthService,
    Embedder,
    EventJudge,
    InstallationStatus,
    MemoryStore,
    MissingSecretError,
    ScheduleStore,
    SecretStore,
    SecretValidator,
    SessionControl,
    SessionSurface,
    SettingsStore,
    TourControl,
    TourDestination,
    TraceEntry,
    TraceSink,
    Wakeup,
)
from server.core.session_tools import register_session_tools
from server.core.schedule_tools import register_schedule_tools
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
    """Couture de la Console web : instantané, Événement, Tours et Session vocale.

    ``snapshot`` est le ``App.snapshot`` injecté à ``start`` : l'adaptateur ne
    connaît ni le Coordinator ni ``SessionState``, et l'instantané est construit
    par la racine de composition au moment de la requête (ADR-0016). ``push``
    est le même ``App.push_event`` que celui du Webhook : l'Événement injecté
    entre dans le Canal d'événements unique, sans file propre à la Console.
    ``tour`` est la couture du **Tour** (ADR-0024) : la Console soumet et annule,
    elle n'atteint jamais le Coordinator. ``session`` est celle de la **Session
    vocale** (ADR-0027) : elle ouvre et ferme un Client Console éphémère.
    """

    async def start(
        self,
        snapshot: Callable[[], Mapping[str, object]],
        push: Callable[[str, str], None] | None = None,
        tour: TourControl | None = None,
        *,
        session: SessionControl | None = None,
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
    """État d'Installation d'une instance (ADR-0026), recalculé à la demande.

    ``setup_required`` appartient toujours au module d'auth : « aucun Compte ».
    L'Installation est plus large — elle se termine quand le Compte existe **et**
    qu'un Secret Gemini utilisable est disponible. La valeur effective suit la
    précédence de la résolution (environnement d'abord, puis magasin), et
    ``restart_required`` naît de l'écart entre cette valeur et celle réellement
    chargée par le processus : la première clé est activée à chaud (ticket de
    revue #166), une clé remplacée ensuite attend un redémarrage.
    """

    def __init__(
        self,
        *,
        auth: AuthService | None,
        secrets: SecretStore | None,
        effective_key: Callable[[], str],
        running_key: Callable[[], str],
    ) -> None:
        self._auth = auth
        self._secrets = secrets
        self._effective_key = effective_key
        self._running_key = running_key

    @property
    def account_exists(self) -> bool:
        """Vrai dès que l'unique Compte existe (ou sans module d'auth)."""
        return self._auth is None or not self._auth.setup_required

    @property
    def gemini_configured(self) -> bool:
        """Vraie si une clé utilisable existe maintenant (runtime ou magasin)."""
        return bool(self._effective_key().strip())

    def status(self) -> InstallationStatus:
        effective = self._effective_key().strip()
        running = self._running_key().strip()
        return InstallationStatus(
            gemini_configured=bool(effective),
            installation_complete=self.account_exists and bool(effective),
            restart_required=effective != running,
            secrets_unreadable=bool(
                self._secrets is not None and self._secrets.unreadable
            ),
        )


@dataclass(frozen=True)
class ExecutionRuntime:
    """Surfaces d'exécution ouvertes ensemble par une clé Gemini utilisable.

    La racine les construit d'un bloc au démarrage (clé déjà présente) ou à la
    fin de l'Installation, puis ``App`` les adopte et démarre leurs pompes dans
    la boucle unique. Aucune surface n'existe sans Gemini : elles sont donc
    toujours absentes ou complètes.
    """

    api_key: str
    transport: Transport | None
    gemini: GeminiLiveAdapter | None
    webhook: WebhookServer | None
    judge: EventJudge | None
    scheduler: ConsolidationScheduler | None
    planificateur: Planificateur | None = None


class RuntimeFactory:
    """Fabrique les surfaces d'exécution au moment où une clé devient utilisable.

    Elle relit la clé **effective** (même précédence que le démarrage) et
    construit transport, Gemini, juge, Consolidation et Webhook avec la
    configuration gelée au démarrage. ``key`` retient la clé réellement chargée
    par le processus : c'est elle que ``Installation`` compare au magasin pour
    décider d'un redémarrage. Une fabrique n'est appelée qu'une fois par
    Installation ; remplacer une clé ensuite passe par le redémarrage.
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        secrets: SecretResolver,
        plugins: PluginRegistry,
        auth: AuthService,
        trace_sink: TraceSink,
        memory: MemoryStore,
        health: HealthState,
        schedule_store: ScheduleStore,
        wakeup: Wakeup,
        occurrence_cell: list[Callable[[OccurrenceReceived], None]],
        schedule_now: Callable[[], datetime],
        live_connect: LiveConnect | None = None,
    ) -> None:
        self._config = config
        self._secrets = secrets
        self._plugins = plugins
        self._auth = auth
        self._trace_sink = trace_sink
        self._memory = memory
        self._health = health
        self._schedule_store = schedule_store
        self._wakeup = wakeup
        self._occurrence_cell = occurrence_cell
        self._schedule_now = schedule_now
        self._live_connect = live_connect
        # Clé chargée au démarrage : vide en Installation, la clé d'origine
        # sinon. ``App`` la remplace après une activation réussie.
        self.key = config.gemini.api_key.strip()

    def resolve_key(self) -> str:
        """Clé Gemini effective maintenant, selon la précédence de l'ADR-0025."""
        return (
            _optional_secret(self._secrets, GEMINI_SECRET)
            or self._config.gemini.api_key
        ).strip()

    def _emit_occurrence(self, occurrence: OccurrenceReceived) -> None:
        """Dépose une Occurrence dans le Canal unique, via la cellule de l'App.

        La fabrique peut être construite avant ``App`` : la cellule n'est
        remplie qu'après, et le pump ne tourne qu'une fois ``App.run`` lancé.
        Une cellule vide (aucune App, test de construction) ignore l'émission
        plutôt que de lever dans la tâche de fond.
        """
        if self._occurrence_cell:
            self._occurrence_cell[0](occurrence)

    def build(self) -> ExecutionRuntime | None:
        """Construit les surfaces, ou ``None`` tant qu'aucune clé n'existe."""
        api_key = self.resolve_key()
        if not api_key:
            return None
        config = self._config
        trace_sink = self._trace_sink
        transport = WebSocketTransport(
            host=config.server.host,
            port=config.server.port,
            server_tools=server_tool_names(self._plugins),
            trace_sink=trace_sink,
            auth=self._auth,
        )
        gemini = GeminiLiveAdapter(
            api_key=api_key,
            model=config.gemini.model,
            voice=config.gemini.voice,
            live_connect=self._live_connect,
            trace_sink=trace_sink,
        )
        webhook = (
            WebhookAdapter(
                host=config.webhook.host,
                port=config.webhook.port,
                trace_sink=trace_sink,
                auth=self._auth,
            )
            if config.webhook.enabled
            else None
        )
        judge = GeminiEventJudge(
            api_key=api_key,
            model=config.gemini.judge_model,
            prompt=load_judge_prompt(),
            trace_sink=trace_sink,
        )
        extractor = GeminiFactExtractor(
            api_key=api_key,
            model=config.memory.consolidation_model,
            trace_sink=trace_sink,
        )
        policy = ConsolidationPolicy(config.memory.consolidation_hours)
        consolidation = Consolidation(
            memory=self._memory,
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
            on_run=lambda report: _record_consolidation(
                self._health, trace_sink, report
            ),
        )
        planificateur = Planificateur(
            store=self._schedule_store,
            now=self._schedule_now,
            timezone=ZoneInfo(config.schedule.timezone),
            sleep=asyncio.sleep,
            emit=self._emit_occurrence,
            trace=lambda kind, payload: emit(
                trace_sink, "planificateur", kind, payload=payload
            ),
            wake=self._wakeup,
        )
        return ExecutionRuntime(
            api_key=api_key,
            transport=transport,
            gemini=gemini,
            webhook=webhook,
            judge=judge,
            scheduler=scheduler,
            planificateur=planificateur,
        )


# Plateforme déclarée par le Client Console : le navigateur, jamais un poste.
CONSOLE_PLATFORM = "web"

# Commandes que la racine route vers le Client Console : testées une seule fois,
# au point de dispatch, puis transmises telles quelles.
_ConsoleCommand = (
    SendAudioToClient | AnnounceSessionStarted | AnnounceSessionEnded
)


@dataclass
class _ConsoleClient:
    """Client Console éphémère hébergé par une conversation vocale (ADR-0027).

    Il vit dans la racine de composition, jamais dans le transport : il ne
    détient aucun Jeton, ne parle pas le Protocole partagé et ne reçoit que
    l'audio de **sa** Session, routé par ``client_id`` comme pour tout Client.
    """

    client_id: str
    client_name: str
    surface: SessionSurface
    #: Identifiant de l'onglet qui l'a ouvert : sa reconnexion remplace son
    #: propre Client abandonné, un second onglet ne le peut pas.
    tab: str
    platform: str = CONSOLE_PLATFORM


def connected_client_ids(
    transport: Transport | None, console_client: _ConsoleClient | None
) -> Sequence[str]:
    """Identifiants des Clients **réellement** connectés, par ordre de préférence.

    Les Clients du transport d'abord, dans leur ordre de connexion : ce sont les
    auditeurs durables. Le Client Console éphémère vient ensuite et ne compte
    que tant que sa surface accepte encore des trames — une conversation vocale
    est une Session, donc un auditeur comme un autre une fois le noyau au
    Repos. La fonction est un simple lecture, appelée à chaque Événement par le
    noyau via une fermeture : l'activation à chaud (remplacement du transport)
    et la mort du Client Console sont donc toujours vues à jour.
    """
    client_ids: list[str] = []
    if transport is not None:
        client_ids.extend(transport.clients)
    if console_client is not None and console_client.surface.alive():
        client_ids.append(console_client.client_id)
    return client_ids


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
        now_context: Callable[[], str] | None = None,
        judge: EventJudge | None = None,
        tools: ToolRegistry | None = None,
        plugins: PluginRegistry | None = None,
        plugin_loader: PluginLoader | None = None,
        plugin_catalogue: PluginCatalogue | None = None,
        plugin_retry_interval: float = 5.0,
        recall_k: int = 5,
        recall_window_turns: int = 3,
        recall_max_distance: float = 0.6,
        scheduler: ConsolidationScheduler | None = None,
        planificateur: Planificateur | None = None,
        webhook: WebhookServer | None = None,
        console: ConsoleServer | None = None,
        runtime_factory: RuntimeFactory | None = None,
        snapshot_config: Mapping[str, object] | None = None,
        tour_destination: TourDestination | None = None,
        installation: Callable[[], InstallationStatus] | None = None,
        account_exists: Callable[[], bool] | None = None,
        health: HealthState | None = None,
        databases: Mapping[str, str | Path] | None = None,
        trace_buffer: Callable[[], Mapping[str, int]] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        # Le transport et Gemini peuvent être **absents** : sans clé Gemini,
        # l'instance est en Installation et ces surfaces d'exécution restent
        # fermées (ADR-0026). Le noyau ne reçoit alors aucun Événement d'eux.
        self._transport = transport
        self._gemini = gemini
        self._clock = clock
        self._trace_sink = trace_sink
        self._memory = memory
        self._scheduler = scheduler
        # Planificateur (ADR-0028) : sa pompe naît avec le runtime Gemini, jamais
        # pendant l'Installation. ``None`` = aucune émission, aucun réveil.
        self._planificateur = planificateur
        self._webhook = webhook
        self._console = console
        # Destination des Tours (ADR-0024) : l'adaptateur Console l'implémente,
        # la racine la lui injecte ici. Absente, un Tour se déroule sans être
        # diffusé — jamais une erreur du noyau.
        self._tour_destination = tour_destination
        # Client Console (ADR-0027) : éphémère, possédé par la racine de
        # composition le temps d'une conversation vocale. Absent hors Session.
        self._console_client: _ConsoleClient | None = None
        # Instantané non sensible de la configuration : la racine de
        # composition choisit explicitement les clés exposées, pour qu'aucun
        # secret ne puisse s'inviter dans l'instantané par accident.
        self._snapshot_config = dict(snapshot_config or {})
        # Diagnostic (#177) : l'Installation et la santé sont des lectures, pas
        # des ports du noyau. La racine injecte les faits ; ``snapshot()`` ne
        # fait que les assembler, sans ``await`` (ADR-0016).
        self._installation = installation
        self._account_exists = account_exists
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
        # Catalogue des dossiers de Plugins : la racine l'adopte au démarrage,
        # après le chargement, pour que la Console décrive aussi les dossiers
        # non listés ou cassés sans jamais muter le registre (ADR-0016).
        self._plugin_catalogue = plugin_catalogue
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
            now_context=now_context,
            # Présence (ADR-0028, #193) : une **fermeture**, pas un instantané,
            # pour que l'activation à chaud du transport et la naissance/mort du
            # Client Console soient lues à chaque Événement. En production, la
            # couture n'est jamais ``None``.
            connected_clients=lambda: connected_client_ids(
                self._transport, self._console_client
            ),
        )
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        # Activation à chaud de la fin d'Installation : une seule à la fois,
        # et les pompes nées après le démarrage rejoignent la même liste que
        # celles de ``run`` (une seule boucle, aucun orphelin — ADR-0005).
        self._runtime_factory = runtime_factory
        self._runtime_lock = asyncio.Lock()
        self._pumps: list[asyncio.Task[None]] = []

    @property
    def scheduler(self) -> ConsolidationScheduler | None:
        return self._scheduler

    @property
    def planificateur(self) -> Planificateur | None:
        """Pompe du Planificateur, ``None`` pendant l'Installation."""
        return self._planificateur

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
        client_rows = [
            {
                "client_id": client.client_id,
                "client_name": client.client_name,
                "platform": client.platform,
            }
            for client in clients.values()
        ]
        # Le Client Console n'appartient pas au transport : l'instantané le
        # montre comme un Client ordinaire tant que la conversation dure, puis
        # il disparaît — aucun Client fantôme (ADR-0027).
        console_client = self._console_client
        if console_client is not None:
            client_rows.append(
                {
                    "client_id": console_client.client_id,
                    "client_name": console_client.client_name,
                    "platform": console_client.platform,
                }
            )
        client_rows.sort(key=lambda row: row["client_name"])
        installation, restart_required = self._installation_block()
        return {
            "session": {
                "state": self._coordinator.state.value,
                "session_id": self._coordinator.session_id,
                "connection_id": self._coordinator.connection_id,
                "active_client_id": self._coordinator.owner_client_id,
                # La Session en cours appartient-elle au Client Console ? Le
                # front s'en sert pour ne pas s'avertir lui-même d'une
                # « Session vocale en cours » (ADR-0027).
                "console": console_client is not None,
            },
            "clients": client_rows,
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

        Une seule source : ``Installation.status()`` pour les faits, complété
        ici de ce que lui seul ne connaît pas — l'existence du Compte et les
        Surfaces réellement ouvertes dans **ce** processus. La liste des clés
        est fermée : un champ accidentel du fournisseur ne passerait pas.
        """
        status = (
            self._installation()
            if self._installation is not None
            else InstallationStatus(
                gemini_configured=self._gemini is not None,
                installation_complete=self._gemini is not None,
                restart_required=False,
                secrets_unreadable=False,
            )
        )
        installation = {
            "account_exists": (
                self._account_exists() if self._account_exists is not None else True
            ),
            "gemini_configured": status.gemini_configured,
            "complete": status.installation_complete,
            "secrets_unreadable": status.secrets_unreadable,
            "surfaces": {
                "gemini": self._gemini is not None,
                "transport": self._transport is not None,
                "webhook": self._webhook is not None,
            },
        }
        return installation, status.restart_required

    def _plugin_summaries(self) -> list[Mapping[str, object]]:
        """Plugins connus, état de chargement et Tools attribués au propriétaire."""
        plugins = self._plugins
        if plugins is None:
            return []
        return [
            {
                "name": name,
                "state": state,
                "tools": list(plugins.tools_of(name)),
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
            await self._console.start(
                self.snapshot, self.push_event, tour=self, session=self
            )
        self._emit("started", payload={"state": self._coordinator.state.value})

        # ``append`` et non réaffectation : une activation à chaud peut déjà
        # avoir ajouté ses pompes à la même liste (aucune orpheline — ADR-0005).
        self._pumps.append(asyncio.create_task(self._pump(self._clock.recv)))
        if self._transport is not None:
            self._pumps.append(asyncio.create_task(self._pump(self._transport.recv)))
        if self._gemini is not None:
            self._pumps.append(asyncio.create_task(self._pump(self._gemini.recv)))
        if self._scheduler is not None:
            # Background task: never on the event-handling path (ADR-0009).
            self._pumps.append(asyncio.create_task(self._scheduler.run()))
        if self._planificateur is not None:
            # Pump du Planificateur : même propriété que la Consolidation, une
            # seule boucle, annulée par le ``finally`` ci-dessous (ADR-0028).
            self._pumps.append(asyncio.create_task(self._planificateur.run()))
        if self._has_pending_plugins():
            # Une panne de plateforme se répare seule : la retente tourne en
            # tâche de fond et les Tools arrivent à la Connexion Live suivante.
            self._pumps.append(asyncio.create_task(self._retry_plugins()))
        try:
            await self._core_loop()
        finally:
            for pump in self._pumps:
                pump.cancel()
            await asyncio.gather(*self._pumps, return_exceptions=True)
            self._pumps = []
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

    async def activate_runtime(self) -> bool:
        """Ouvre les surfaces d'exécution dès que l'Installation se termine.

        Appelée par la route des Secrets quand la première clé Gemini utilisable
        vient d'être enregistrée (story 12). Les adaptateurs sont construits par
        la fabrique de la racine, démarrés ici, puis adoptés : les pompes du
        transport, de Gemini et de la Consolidation rejoignent la boucle unique.
        Aucun redémarrage de processus, aucun accès de l'adaptateur Console au
        Coordinator. Rend ``True`` quand l'activation a eu lieu.
        """
        factory = self._runtime_factory
        if factory is None or self._gemini is not None:
            return False
        async with self._runtime_lock:
            if self._gemini is not None:
                return False
            runtime = factory.build()
            if runtime is None:
                return False
            try:
                if runtime.transport is not None:
                    await runtime.transport.start()
                if runtime.webhook is not None:
                    await runtime.webhook.start(self.push_event)
            except Exception:
                # Une surface à moitié liée ne doit pas rester derrière elle :
                # la clé reste enregistrée, une nouvelle tentative repartira.
                if runtime.transport is not None:
                    await runtime.transport.stop()
                if runtime.webhook is not None:
                    await runtime.webhook.stop()
                raise
            await self._adopt_runtime(runtime)
            factory.key = runtime.api_key
            self._emit(
                "RuntimeActivated",
                payload={
                    "transport": runtime.transport is not None,
                    "webhook": runtime.webhook is not None,
                    "scheduler": runtime.scheduler is not None,
                },
            )
            return True

    async def _adopt_runtime(self, runtime: ExecutionRuntime) -> None:
        """Arrête puis remplace les surfaces, et démarre leurs pompes.

        Sur une instance en Installation, aucune surface n'est ouverte : l'arrêt
        est un no-op. Il reste écrit pour qu'un remplacement défensif ne laisse
        jamais deux serveurs liés au même port.
        """
        if self._transport is not None:
            await self._transport.stop()
        if self._webhook is not None:
            await self._webhook.stop()
        self._transport = runtime.transport
        self._gemini = runtime.gemini
        self._webhook = runtime.webhook
        self._scheduler = runtime.scheduler
        self._planificateur = runtime.planificateur
        self._coordinator.set_judge(runtime.judge)
        if runtime.transport is not None:
            self._pumps.append(
                asyncio.create_task(self._pump(runtime.transport.recv))
            )
        if runtime.gemini is not None:
            self._pumps.append(
                asyncio.create_task(self._pump(runtime.gemini.recv))
            )
        if runtime.scheduler is not None:
            self._pumps.append(asyncio.create_task(runtime.scheduler.run()))
        if runtime.planificateur is not None:
            self._pumps.append(
                asyncio.create_task(runtime.planificateur.run())
            )

    async def _warmup_memory(self) -> None:
        warmup = getattr(self._memory, "warmup", None)
        if warmup is not None:
            await warmup()

    async def _load_plugins(self) -> None:
        """Charge les plugins activés dans le registre, avant tout service."""
        if self._plugins is None:
            return
        if self._plugin_loader is not None:
            await self._plugin_loader.load(
                self._plugins, push_event=self.push_event
            )
        self._adopt_catalogue()

    def _adopt_catalogue(self) -> None:
        """Verse les métadonnées des dossiers que le chargeur n'a pas décrits.

        Seuls les Plugins **non listés ou cassés** arrivent ici : le chargeur a
        déjà décrit ceux qu'il a ouverts. Le registre reste la source unique de
        ce que la Console affiche, et l'adaptateur Console ne le mute jamais.
        """
        catalogue = self._plugin_catalogue
        registry = self._plugins
        if catalogue is None or registry is None:
            return
        for entry in catalogue.entries():
            if registry.description_of(entry.name) is None:
                registry.describe(entry.description)

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

    def push_occurrence(self, occurrence: OccurrenceReceived) -> None:
        """Dépose une Occurrence du Planificateur dans le **même** Canal.

        Couture ``emit`` du Planificateur (ADR-0028) : le noyau l'enfile comme
        tout fait, la juge au Repos sans passer par le juge, et attend un Client
        connecté. Aucune seconde file.
        """
        self._events.put_nowait(occurrence)

    def submit_tour(self, tour_id: str, text: str) -> None:
        """Dépose un Tour texte dans le Canal (couture ``TourControl``, ADR-0024).

        La Console ne connaît ni le Coordinator ni l'état de Session : elle
        demande, le noyau décide — y compris de refuser quand il est occupé.
        """
        self._events.put_nowait(TourRequested(tour_id=tour_id, text=text))

    def cancel_tour(self, tour_id: str) -> None:
        """Dépose l'annulation d'un Tour (flux SSE fermé par le navigateur)."""
        self._events.put_nowait(TourCanceled(tour_id=tour_id))

    # --- Client Console (couture ``SessionControl``, ADR-0027) --------------

    def open_session(
        self,
        *,
        client_id: str,
        client_name: str,
        surface: SessionSurface,
        tab: str = "",
    ) -> bool:
        """Crée le Client Console et demande une Session, sans jamais préempter.

        Seuls un Client Console **abandonné** — surface morte, ou reconnexion du
        même onglet après une coupure que le serveur n'a pas encore constatée —
        est remplacé (``ClientDisconnected`` -> ``CLIENT_GONE``). Un second
        onglet, lui, est refusé : la Session d'un autre Client n'est jamais
        volée.

        La pré-vérification est synchrone : le noyau est occupé (Tour ou Session
        d'un autre Client), ou l'Installation n'a pas encore de clé Gemini. Le
        refus est déposé sur la surface avec un code stable, aucun Client n'est
        créé. ``SessionRequested(no_preempt=True)`` ferme ensuite la course avec
        un occupant arrivé entre-temps.
        """
        if self._gemini is None:
            surface.refused("installation_incomplete")
            return False
        existing = self._console_client
        if existing is None:
            busy = self._coordinator.busy_reason()
            if busy:
                surface.refused(busy)
                return False
        elif not self._is_abandoned(existing, tab):
            surface.refused("session_busy")
            return False
        else:
            # Retiré de l'instantané avant l'Événement : l'ordre du Canal
            # garantit que la Session fantôme meurt avant la nouvelle demande.
            self._console_client = None
            self._events.put_nowait(
                ClientDisconnected(client_id=existing.client_id)
            )
        self._console_client = _ConsoleClient(
            client_id=client_id,
            client_name=client_name,
            surface=surface,
            tab=tab,
        )
        # Naissance au démarrage : le noyau voit un Client ordinaire, puis une
        # demande de Session qui porte son ``client_id``.
        self._events.put_nowait(
            ClientRegistered(
                client_id=client_id,
                client_name=client_name,
                platform=CONSOLE_PLATFORM,
            )
        )
        self._events.put_nowait(
            SessionRequested(client_id=client_id, no_preempt=True)
        )
        return True

    @staticmethod
    def _is_abandoned(client: _ConsoleClient, tab: str) -> bool:
        """Vrai quand ce Client Console ne peut plus être celui d'un onglet vivant."""
        if not client.surface.alive():
            # Le socket est déjà refermé : seule la purge du noyau manque.
            return True
        # Surface encore ouverte : seul le même onglet peut la remplacer, car
        # une coupure réseau silencieuse ne se voit pas côté serveur.
        return tab != "" and client.tab == tab

    def send_audio(self, client_id: str, data: bytes) -> None:
        """Dépose l'audio du Client Console ; le noyau l'ignore avant ``LlmOpened``."""
        client = self._console_client
        if client is None or client.client_id != client_id:
            return
        self._events.put_nowait(ClientAudio(data=data))

    def close_session(self, client_id: str) -> None:
        """Le Client Console disparaît : ``ClientDisconnected`` termine la Session."""
        client = self._console_client
        if client is None or client.client_id != client_id:
            return
        # Retiré de l'instantané avant l'Événement : aucun Client fantôme ne
        # survit à la fermeture de l'onglet.
        self._console_client = None
        self._events.put_nowait(ClientDisconnected(client_id=client_id))

    async def _pump(self, recv: Callable[[], Awaitable[Event]]) -> None:
        while True:
            await self._events.put(await recv())

    async def _core_loop(self) -> None:
        while True:
            event = await self._events.get()
            for command in await self._coordinator.handle(event):
                await self._dispatch(command)
            self._notify_console(event)
            if isinstance(event, ShutdownRequested):
                return

    def _notify_console(self, event: Event) -> None:
        """Pousse ``ready`` et les transcriptions au Client Console (ADR-0027).

        Appelé **après** le traitement de l'Événement : ``ready`` suit
        ``LlmOpened`` (la Connexion Live est réellement ouverte), et une
        transcription n'est adressée qu'à la Session que ce Client possède.
        """
        client = self._console_client
        if client is None:
            return
        if self._coordinator.session_client_id != client.client_id:
            return
        if isinstance(event, LlmOpened):
            client.surface.ready()
        elif isinstance(event, LlmTranscription):
            client.surface.transcript(event.role.value, event.text)

    async def _dispatch(self, command: Command) -> None:
        if isinstance(command, Trace):
            if command.kind == "SessionClosed":
                self._close_console_session(command)
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
            if self._deliver_to_console(command):
                return
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
            # transport le traduit en message ``Error`` existant. Pour le Client
            # Console, le refus part en trame JSON et le Client n'est jamais né.
            console = self._console_client
            if console is not None and command.client_id == console.client_id:
                console.surface.refused(command.reason)
                self._console_client = None
                return
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

    def _deliver_to_console(self, command: _ConsoleCommand) -> bool:
        """Route l'audio et la fin vers le Client Console, jamais le transport.

        Rend ``True`` quand la Commande appartenait au Client Console. Le
        ``client_id`` fait foi, comme pour tout Client : une Session d'un autre
        appareil continue de passer par le transport. Le type est déjà restreint
        par l'appelant : la famille de Commandes n'est testée qu'une fois.
        """
        client = self._console_client
        if client is None or command.client_id != client.client_id:
            return False
        if isinstance(command, SendAudioToClient):
            client.surface.audio(command.data)
        elif isinstance(command, AnnounceSessionEnded):
            client.surface.ended(command.reason.name)
        # ``AnnounceSessionStarted`` est un message du Protocole partagé : le
        # navigateur n'en a pas besoin, ``ready`` puis l'audio suffisent.
        return True

    def _close_console_session(self, command: Trace) -> None:
        """La Session est close : le Client Console meurt, aucun Client fantôme.

        La fin est déposée sur la surface, qui garantit elle-même l'envoi unique
        et terminal : ``AnnounceSessionEnded`` et cette trace convergent ici sans
        second drapeau.
        """
        client = self._console_client
        if client is None:
            return
        if command.payload.get("client_id") != client.client_id:
            return
        reason = str(command.payload.get("reason", "NORMAL"))
        client.surface.ended(reason)
        self._console_client = None

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


#: Noms français sans dépendre de la locale du processus : un conteneur minimal
#: n'installe pas forcément les locales, et le prompt doit rester stable.
_FRENCH_WEEKDAYS = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
)
_FRENCH_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def format_now_context(moment: datetime, timezone_name: str) -> str:
    """Rend la section ``[CONTEXTE TEMPOREL]`` dans le fuseau de l'instance.

    ``moment`` est un instant (naïf, il est lu comme UTC) ; la conversion vers
    ``timezone_name`` suit les règles IANA, passage d'heure d'été compris. La
    seconde est incluse : le modèle convertit « dans 10 min » sans deviner.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(ZoneInfo(timezone_name))
    weekday = _FRENCH_WEEKDAYS[local.weekday()]
    month = _FRENCH_MONTHS[local.month - 1]
    return (
        f"{NOW_CONTEXT_HEADER}\n"
        f"Nous sommes le {weekday} {local.day} {month} {local.year}, "
        f"il est {local:%H:%M:%S} ({timezone_name})."
    )


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
    wall_clock: Callable[[], datetime] | None = None,
    wakeup: Wakeup | None = None,
    restart: Callable[[], None] | None = None,
) -> App:
    """Construit explicitement : config → trace → adaptateurs → Coordinator.

    ``secrets`` résout les secrets des plugins et la clé Gemini ; par défaut un
    résolveur à trois couches (environnement du processus > magasin > ``.env``)
    applique la précédence de l'ADR-0025. ``settings`` remplace le magasin
    construit depuis ``config.settings`` (seam de test). ``secret_validator``
    remplace la validation réelle d'une clé Gemini.

    Sans clé Gemini utilisable, l'instance démarre en **Installation** : le
    transport, le Webhook et Gemini ne sont pas construits, la Console et ses
    routes de Réglages et de Secrets restent servies (ADR-0026).

    ``live_connect``, ``clock``, ``embedder``, ``auth_now``, ``monotonic``,
    ``settings`` et ``secret_validator`` sont les coutures laissées ouvertes au
    harnais de test serveur : le serveur garde tous ses autres adaptateurs réels
    (Console, auth, mémoire SQLite), et la configuration effective se remplace
    sans relancer le processus. ``restart`` remplace la sortie propre demandée
    par l'application des Réglages : le harnais observe le redémarrage sans
    tuer pytest. ``wall_clock`` est l'horloge murale du contexte temporel
    (défaut : ``datetime.now(UTC)``) : un test fige l'heure sans attendre ; le
    Planificateur et ``create_schedule`` lisent la même horloge, donc un rappel
    « dans N » se crée et sonne dans le temps simulé. ``wakeup`` remplace le
    réveil asyncio du pump (seam de test) sans changer le Canal.
    """
    # Magasin de réglages et de Secrets (ADR-0025) : base dédiée à côté de la
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
    # valeur que le processus n'utilise pas (ADR-0025).
    config, applied_settings = config_with_stored_settings(
        config,
        store=_stored_setting_values(settings_store),
        process_env=process_env,
    )

    # Horloge murale unique (#192) : le contexte temporel, ``create_schedule`` et
    # le pump du Planificateur lisent la **même** heure. Un test qui fige
    # l'horloge voit donc le rappel se créer et sonner dans le temps simulé.
    wall_clock = (
        wall_clock
        if wall_clock is not None
        else (lambda: datetime.now(timezone.utc))
    )
    timezone_name = config.schedule.timezone
    schedule_zone = ZoneInfo(timezone_name)

    def schedule_now() -> datetime:
        """Instant courant dans le fuseau du Réglage, toujours aware."""
        moment = wall_clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(schedule_zone)

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

    # Planificateur (ADR-0028) : base dédiée, derrière le port ``ScheduleStore``.
    # Le store est construit même en Installation — les Planifications y
    # persistent sans runtime — et sa connexion reste paresseuse.
    schedule_store = SqliteScheduleStore(
        db_path=config.schedule.db_path, trace_sink=trace_sink
    )
    # Réveil du pump : partagé par les Tools (``on_change``) et la boucle, pour
    # qu'une Planification plus proche sonne à l'heure sans attendre l'ancienne
    # échéance. Un test peut l'injecter pour piloter l'attente sans temps réel.
    schedule_wakeup = wakeup if wakeup is not None else AsyncioWakeup()
    # Cellule d'émission : remplie avec ``App.push_occurrence`` après la
    # construction de l'App, comme ``app_cell``. La fabrique et l'App se
    # construisent dans cet ordre sans que le Planificateur connaisse l'App.
    occurrence_cell: list[Callable[[OccurrenceReceived], None]] = []

    # Une seule fabrique construit les surfaces d'exécution : au démarrage
    # quand une clé existe déjà, et à la fin de l'Installation sinon (story 12).
    # Elle garde la configuration gelée et retient la clé réellement chargée.
    runtime_factory = RuntimeFactory(
        config=config,
        secrets=secret_resolver,
        plugins=plugins,
        auth=auth,
        trace_sink=trace_sink,
        memory=memory,
        health=health,
        schedule_store=schedule_store,
        wakeup=schedule_wakeup,
        occurrence_cell=occurrence_cell,
        schedule_now=schedule_now,
        live_connect=live_connect,
    )
    boot_runtime = runtime_factory.build() if gemini_enabled else None
    if boot_runtime is None:
        transport: WebSocketTransport | None = None
        gemini: GeminiLiveAdapter | None = None
        judge: GeminiEventJudge | None = None
        scheduler: ConsolidationScheduler | None = None
        planificateur: Planificateur | None = None
        webhook: WebhookAdapter | None = None
    else:
        transport = boot_runtime.transport
        gemini = boot_runtime.gemini
        judge = boot_runtime.judge
        scheduler = boot_runtime.scheduler
        planificateur = boot_runtime.planificateur
        webhook = boot_runtime.webhook

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
    # Planificateur (#194/#195/#197) : les quatre Tools, et le réveil du pump
    # après chaque écriture réussie, pour qu'une échéance plus proche que celle
    # attendue sonne à l'heure. Les règles s'interprètent dans le fuseau du
    # Réglage, déjà validé par la configuration.
    register_schedule_tools(
        plugins.tools,
        schedule_store,
        now=schedule_now,
        timezone=schedule_zone,
        on_change=schedule_wakeup.notify,
    )
    plugin_loader = PluginLoader(
        directory=config.plugins.directory,
        state_path=config.plugins.state_path,
        secrets=secret_resolver,
        trace_sink=trace_sink,
    )
    # Catalogue des Plugins (ticket #176) : il décrit tous les dossiers, y
    # compris cassés ou non listés. La racine l'adopte au démarrage ; la
    # Console ne fait que lire le registre (ADR-0016).
    plugin_catalogue = PluginCatalogue(config.plugins.directory)
    # Gestion des Plugins (ticket #176) : le service écrit l'état et les
    # Secrets, sans jamais recharger un plugin à chaud.
    plugin_admin = PluginAdmin(
        catalogue=plugin_catalogue,
        state=PluginStateStore(config.plugins.state_path),
        registry=plugins,
        loader=plugin_loader,
        secrets=settings_store,
    )

    # État d'Installation : Compte + Secret Gemini utilisable (ADR-0026).
    # ``runtime_factory.key`` suit la clé chargée : l'activer à chaud la met à
    # jour, donc ``restart_required`` retombe sans redémarrage.
    installation = Installation(
        auth=auth,
        secrets=settings_store,
        effective_key=runtime_factory.resolve_key,
        running_key=lambda: runtime_factory.key,
    )
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
    # Cellule de l'App, remplie juste après sa construction : la route des
    # Secrets déclenche l'activation à chaud de la fin d'Installation sans que
    # l'adaptateur connaisse jamais le Coordinator (ADR-0016).
    app_cell: list[App] = []

    async def activate_after_install() -> None:
        if app_cell:
            await app_cell[0].activate_runtime()

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
            installed=activate_after_install,
            # Fil de la Conversation : lecture paginée du Journal, bornée à la
            # rétention de la mémoire courte (ADR-0024).
            thread=ThreadView(
                memory=memory,
                user_id=config.memory.user_id,
                retention_days=config.memory.journal_retention_days,
            ),
            plugin_admin=plugin_admin,
        )

    # Contexte temporel : la racine rend la section à chaque ouverture de
    # Session, Tour ou Annonce, dans le fuseau du Réglage. Le noyau ne reçoit
    # qu'une callable, jamais une horloge ni ``zoneinfo`` (pureté du noyau).
    def render_now_context() -> str:
        return format_now_context(wall_clock(), timezone_name)

    app = App(
        transport=transport,
        gemini=gemini,
        clock=clock,
        trace_sink=trace_sink,
        memory=memory,
        user_id=config.memory.user_id,
        system_prompt=load_system_prompt(),
        announce_prompt=load_announce_prompt(),
        now_context=render_now_context,
        judge=judge,
        tools=plugins.tools,
        plugins=plugins,
        plugin_loader=plugin_loader,
        plugin_catalogue=plugin_catalogue,
        runtime_factory=runtime_factory,
        recall_k=config.memory.recall_k,
        recall_window_turns=config.memory.recall_window_turns,
        recall_max_distance=config.memory.recall_max_distance,
        scheduler=scheduler,
        planificateur=planificateur,
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
        installation=installation.status,
        account_exists=lambda: installation.account_exists,
        health=health,
        databases={
            "memory": config.memory.db_path,
            "auth": config.auth.db_path,
            "settings": config.settings.db_path,
            "schedule": config.schedule.db_path,
        },
        trace_buffer=(
            console_trace_buffer.stats
            if console_trace_buffer is not None
            else None
        ),
        monotonic=monotonic,
    )
    # Les cellules sont remplies une fois l'App construite : un PUT qui demande
    # le redémarrage dépose ``ShutdownRequested`` dans la boucle unique, et la
    # première clé Gemini active les surfaces d'exécution sur place.
    restart_cell.append(app.request_shutdown)
    app_cell.append(app)
    # Le Planificateur dépose ses Occurrences dans le Canal unique de l'App.
    occurrence_cell.append(app.push_occurrence)
    return app


def _stored_setting_values(store: SecretStore) -> dict[str, str]:
    """Valeurs de réglages du magasin, quand l'objet en implémente le port.

    ``SecretStore`` est le port minimal accepté par ``build_app`` ; le magasin
    réel implémente aussi ``SettingsStore``. Un faux qui ne le fait pas rend un
    magasin vide : les défauts s'appliquent, sans erreur.
    """
    return dict(store.values()) if isinstance(store, SettingsStore) else {}


def _settings_store(store: SecretStore) -> SettingsStore | None:
    """Le magasin de réglages, ou ``None`` si l'objet n'en est pas un.

    Réglages et Secrets partagent une base en production ; un test peut
    injecter un faux qui ne porte que les Secrets. Les routes répondent alors
    ``503 settings_unavailable`` plutôt que d'inventer un magasin.
    """
    return store if isinstance(store, SettingsStore) else None


def _optional_secret(resolver: SecretResolver, name: str) -> str | None:
    """Résout un Secret facultatif : son absence n'est pas une erreur ici."""
    try:
        return resolver.resolve(name)
    except MissingSecretError:
        return None
