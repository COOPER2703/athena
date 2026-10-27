from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Protocol

from server import __version__
from server.adapters.auth import SqliteAuthStore
from server.adapters.console import ConsoleAdapter
from server.adapters.console.trace_buffer import TraceBuffer
from server.adapters.gemini import GeminiEventJudge, GeminiLiveAdapter
from server.adapters.memory import (
    GeminiFactExtractor,
    SentenceTransformerEmbedder,
    SqliteMemoryStore,
)
from server.adapters.plugins import PluginLoader
from server.adapters.trace import emit
from server.adapters.transport import ClientConnection, WebSocketTransport
from server.adapters.webhook import WebhookAdapter
from server.app.clock import AsyncioClock
from server.app.config import AppConfig, default_env_file, merged_environment
from server.app.secrets import EnvSecretResolver, SecretResolver
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
    OpenLiveConnection,
    SendAudioToClient,
    SendAudioToLlm,
    SendAnnouncementToLlm,
    SendMemoryToLlm,
    SendToolResultToLlm,
    StartIdleTimer,
    Trace,
)
from server.core.coordinator import Coordinator
from server.core.events import Event, EventReceived, ShutdownRequested
from server.core.memory_tools import register_memory_tools
from server.core.plugins import PluginRegistry
from server.core.ports import AuthService, EventJudge, MemoryStore, TraceSink
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


class ConsoleServer(Protocol):
    """Couture de la Console web : instantané de lecture et injection d'Événement.

    ``snapshot`` est le ``App.snapshot`` injecté à ``start`` : l'adaptateur ne
    connaît ni le Coordinator ni ``SessionState``, et l'instantané est construit
    par la racine de composition au moment de la requête (ADR-0016). ``push``
    est le même ``App.push_event`` que celui du Webhook : l'Événement injecté
    entre dans le Canal d'événements unique, sans file propre à la Console.
    """

    async def start(
        self,
        snapshot: Callable[[], Mapping[str, object]],
        push: Callable[[str, str], None] | None = None,
    ) -> None: ...

    async def stop(self) -> None: ...


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
        transport: Transport,
        gemini: GeminiLiveAdapter,
        clock: AsyncioClock,
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
        auth: AuthService | None = None,
        snapshot_config: Mapping[str, object] | None = None,
    ) -> None:
        self._transport = transport
        self._gemini = gemini
        self._clock = clock
        self._trace_sink = trace_sink
        self._memory = memory
        self._scheduler = scheduler
        self._webhook = webhook
        self._console = console
        # Le module d'auth est possédé par la racine de composition. L'App ne
        # s'en sert qu'au démarrage, pour publier le Code d'installation une
        # seule fois tant qu'aucun Compte n'existe ; les surfaces le reçoivent
        # par leur constructeur.
        self._auth = auth
        # Instantané non sensible de la configuration : la racine de
        # composition choisit explicitement les clés exposées, pour qu'aucun
        # secret ne puisse s'inviter dans l'instantané par accident.
        self._snapshot_config = dict(snapshot_config or {})
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

    def snapshot(self) -> Mapping[str, object]:
        """Instantané de lecture de l'état du noyau, prêt pour le JSON.

        Construit **à la demande** par la racine de composition, jamais poussé
        par le noyau : la Console lit un instantané de lecture et ne peut pas
        atteindre le Coordinator (ADR-0016). Aucun secret : ``config`` ne porte
        que les valeurs explicitement injectées par ``build_app``.
        """
        clients = self._transport.clients
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
        }

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
        await self._load_plugins()
        await self._warmup_memory()
        # Le Code d'installation entre dans le flux de traces (donc dans le
        # journal) avant que la moindre surface ne serve : un visiteur ne peut
        # pas revendiquer l'instance avant que l'admin n'ait lu le code. Sans
        # Compte seulement, et une fois par démarrage.
        if self._auth is not None:
            self._auth.announce_installation_code()
        await self._transport.start()
        if self._webhook is not None:
            await self._webhook.start(self.push_event)
        if self._console is not None:
            await self._console.start(self.snapshot, self.push_event)
        self._emit("started", payload={"state": self._coordinator.state.value})

        pumps = [
            asyncio.create_task(self._pump(self._transport.recv)),
            asyncio.create_task(self._pump(self._gemini.recv)),
            asyncio.create_task(self._pump(self._clock.recv)),
        ]
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
                SendAnnouncementToLlm,
                SendToolResultToLlm,
            ),
        ):
            await self._gemini.execute(command)
            return
        if isinstance(
            command,
            (SendAudioToClient, AnnounceSessionStarted, AnnounceSessionEnded),
        ):
            await self._transport.execute(command)
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


def build_app(
    config: AppConfig,
    *,
    secrets: SecretResolver | None = None,
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
) -> App:
    """Construit explicitement : config → trace → adaptateurs → Coordinator.

    ``secrets`` résout les secrets des plugins (variables
    ``ATHENA_PLUGIN_<PLUGIN>_<SECRET>``) ; par défaut la même vue fusionnée que
    le reste de la configuration : fichier ``.env`` (celui du répertoire
    courant, ou ``env_file``) recouvert par l'environnement du processus. Un
    jeton posé dans le ``.env`` est donc visible des plugins sans être passé par
    ``AppConfig``. La racine de composition fournit aussi le ``TraceSink`` au
    chargeur, pour que les états de plugin entrent dans le flux unique.
    """
    # Tampon de traces de la Console : consommateur borné du flux unique, gardé
    # seulement quand la surface est servie. Le fan-out y pousse chaque entrée,
    # tous niveaux confondus (ADR-0004).
    console_trace_buffer = (
        TraceBuffer(config.console.trace_buffer) if config.console.enabled else None
    )
    trace_sink = build_trace_sink(
        debug_decisions=config.debug_decisions,
        log_dir=config.log_dir,
        level=config.log_level,
        extra_sinks=[console_trace_buffer] if console_trace_buffer else [],
    )
    install_logging_bridge(
        trace_sink,
        level="DEBUG" if config.debug_decisions else config.log_level,
    )

    # Authentification applicative : base dédiée, comme la mémoire, et jamais
    # de secret par défaut. Les surfaces reçoivent ce service par leur
    # constructeur ; seule la Console l'utilise dans ce lot.
    auth = SqliteAuthStore(
        db_path=config.auth.db_path,
        trace_sink=trace_sink,
        ticket_ttl_seconds=config.auth.ticket_days * 24 * 60 * 60,
        ticket_refresh_seconds=config.auth.ticket_refresh_seconds,
        password_min_length=config.auth.password_min_length,
        login_max_attempts=config.auth.login_max_attempts,
        login_block_seconds=config.auth.login_block_minutes * 60,
        setup_max_attempts=config.auth.setup_max_attempts,
    )

    plugin_secrets = (
        secrets
        if secrets is not None
        else EnvSecretResolver(
            merged_environment(
                env,
                env_file=env_file if env_file is not None else default_env_file(),
            )
        )
    )

    # Tools du noyau et Tools de plugin partagent un seul registre : celui du
    # Plugin Registry. Les Tools du noyau y entrent au propriétaire ``core``,
    # les plugins activés s'y ajoutent au démarrage. Le registre est construit
    # avant le transport pour que ``Registered.server_tools`` le lise à chaque
    # enregistrement, plugin chargé ou non.
    plugins = PluginRegistry()

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
        api_key=config.gemini.api_key,
        model=config.gemini.model,
        voice=config.gemini.voice,
        trace_sink=trace_sink,
    )
    clock = AsyncioClock()

    embedder = SentenceTransformerEmbedder(config.memory.embed_model)
    memory = SqliteMemoryStore(
        db_path=config.memory.db_path,
        embedder=embedder,
        short_term_window=config.memory.short_term_window,
        recent_events_window=config.memory.recent_events_window,
        journal_retention_days=config.memory.journal_retention_days,
    )

    extractor = GeminiFactExtractor(
        api_key=config.gemini.api_key,
        model=config.memory.consolidation_model,
        trace_sink=trace_sink,
    )
    judge = GeminiEventJudge(
        api_key=config.gemini.api_key,
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
        on_run=lambda report: _trace_consolidation(trace_sink, report),
    )

    # Les Tools du noyau entrent au propriétaire ``core`` ; les plugins activés
    # s'y ajoutent au démarrage.
    register_memory_tools(
        plugins.tools,
        memory,
        config.memory.user_id,
        forget_max_distance=config.memory.recall_max_distance,
    )
    plugin_loader = PluginLoader(
        directory=config.plugins.directory,
        state_path=config.plugins.state_path,
        secrets=plugin_secrets,
        trace_sink=trace_sink,
    )

    webhook: WebhookAdapter | None = None
    if config.webhook.enabled:
        webhook = WebhookAdapter(
            host=config.webhook.host,
            port=config.webhook.port,
            trace_sink=trace_sink,
            auth=auth,
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
        )

    return App(
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
        auth=auth,
        snapshot_config={
            "model": config.gemini.model,
            "voice": config.gemini.voice,
            "user_id": config.memory.user_id,
            "memory_db": config.memory.db_path,
            "log_dir": config.log_dir,
            "version": __version__,
        },
    )
