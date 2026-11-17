"""Harnais de test serveur : le vrai serveur, sur des ports éphémères.

C'est **le seam unique** des tests d'intégration serveur. Une seule fixture,
``running_server()``, démarre la vraie racine de composition
(``server.app.root.build_app``) avec ses adaptateurs réels — transport
WebSocket, Console web, authentification, mémoire SQLite, traces — et un modèle
Live de test injecté à la couture ``live_connect`` de ``GeminiLiveAdapter``.
Chaque test reçoit des répertoires temporaires isolés (bases, plugins, journaux)
et aucun secret réel n'est requis.

La fixture est un gestionnaire de contexte asynchrone, utilisable dans le style
du dépôt (un test synchrone ouvre ``asyncio.run(scenario())``) :

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            async with server.client() as client:
                await client.start_session()
                server.model.reply("Bonjour", audio=b"…")
                await client.send_audio(b"micro")
                assert await client.wait_message(Audio) == Audio(data=b"…")

    asyncio.run(scenario())

API publique
============

``running_server(root, *, static_dir=None, prepare=None)``
    Démarre le serveur complet. ``root`` est le répertoire temporaire du test
    (``tmp_path``) ; tout l'état vit dessous. ``static_dir`` remplace le build
    de la SPA servi par la Console. ``prepare(paths)`` s'exécute avant le
    démarrage avec les chemins déjà calculés (déposer un plugin, un build, un
    fichier d'état). ``gemini_key`` règle la clé lue au démarrage (``""`` pour
    une instance en Installation). ``env`` remplace l'environnement du
    processus pour la configuration effective, ``secrets`` remplace le
    résolveur à trois couches, ``settings`` remplace le magasin de réglages,
    ``webhook`` ouvre aussi la surface Webhook, et ``secret_validator``
    remplace la validation réelle d'une clé Gemini. ``restart_on_apply``,
    ``restart_command`` et ``restart`` règlent l'application des Réglages : la
    callable injectée remplace la sortie du processus, pour qu'un test observe
    le redémarrage sans tuer pytest. Le serveur est arrêté proprement à la
    sortie du bloc, et le logging racine est restauré.

``AthenaServer`` (ce que rend la fixture)
    ``root`` / ``url`` / ``transport_url``  : le répertoire et les deux surfaces.
    ``paths``                    : bases, plugins, journaux (``ServerPaths``).
    ``model``                    : le faux modèle Live (``FakeLiveModel``).
    ``clock``                    : l'horloge virtuelle (``ManualClock``), qui
                                   règle aussi le temps de l'authentification.
    ``validator``                : le faux validateur de Secrets (aucun réseau).
    ``anonymous()``              : client HTTP sans cookie ni ``Origin``.
    ``console()``                : client HTTP « navigateur » (cookie + ``Origin``)
                                   avant installation, pour installer ou se connecter.
    ``install()``                : crée le Compte par la vraie route et rend le
                                   client HTTP authentifié (Ticket Console).
    ``access_token()``           : crée un Jeton d'accès par ``POST /api/tokens``.
    ``client()``                 : connexion au transport audio, enregistrée,
                                   avec les messages du Protocole partagé.
    ``trace_stream()``           : abonnement au WebSocket ``/api/stream`` de la
                                   Console (``hello``, backlog, trames suivantes).
    ``read_sse(response)``       : lit un flux ``text/event-stream`` (Tours texte).
    ``trace_log()`` / ``read_log()`` : le journal ``logs/server.log`` déjà lu.
    ``status()``                  : l'instantané étendu de ``GET /api/status``.
    ``query(db, sql)``           : lecture SQL d'une des bases du serveur.
    ``emit_log(logger, message, level=…)``: émet une entrée de logging dans le flux.
    ``wait_until()``             : attente courte d'une condition synchrone.

``FakeLiveModel`` — le modèle Live de test
    ``reply(text="", audio=b"…", heard="…")`` : scripte un tour (texte, audio,
    et/ou transcription de ce que l'utilisateur a dit).
    ``call_tool(name, arguments)`` : scripte un appel de Tool.
    ``fail()`` : scripte une défaillance (``go_away``) de la Connexion Live.
    ``delay``                       : ralentit les tours scriptés (annulation).
    ``received_audio`` / ``received_texts`` : ce que le serveur a envoyé.
    ``tool_results``               : les résultats de Tools reçus du serveur.
    ``configs`` / ``declarations()`` / ``instruction()`` : la Connexion Live.

    Un tour scripté part à l'entrée suivante du modèle : audio montant, ou
    contenu texte avec ``turn_complete=True``. L'injection de mémoire
    (``turn_complete=False``), elle, ne déclenche rien — comme chez Gemini.

``ManualClock`` — l'horloge du serveur
    ``advance(seconds)`` avance le temps virtuel et dépose les ``TimerFired``
    dus ; ``scheduled`` / ``pending`` / ``next_timeout()`` les inspectent. Les
    sessions ne s'expirent donc jamais toutes seules dans un test, et un Ticket
    Console se périme quand on avance au-delà de sa durée.

Migration
=========
Les tests d'intégration qui montaient leur propre ``build_app`` + ports + clients
HTTP utilisent cette fixture (``test_app_root``, ``test_app_auth``,
``test_console_stream``, l'instantané de ``test_app_integration``). Les tests
unitaires du noyau et des adaptateurs continuent de se construire à la main :
ils ne démarrent pas le serveur complet.

Le Code d'installation a disparu (ADR-0025) : ``install()`` crée l'unique
Compte sans code et garde le Ticket Console.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import socket
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Mapping

import aiohttp
import sqlite_vec
import websockets

from conftest import TEST_PASSWORD, wait_until
from protocol import (
    Audio,
    Register,
    Registered,
    SessionStart,
    decode,
    encode,
)
from server.adapters.console.contract import HEALTH_PATH, STREAM_PATH, TOKENS_PATH
from server.app.config import (
    AppConfig,
    AuthConfig,
    ConsoleConfig,
    GeminiConfig,
    MemoryConfig,
    PluginsConfig,
    ServerConfig,
    SettingsConfig,
    WebhookConfig,
    overlay_environment,
)
from server.app.root import App, build_app
from server.core.events import TimerFired
from server.core.ports import SecretResolver, SecretStore, SecretValidator

# Logs du harnais : même niveau que la configuration par défaut.
_LOG_LEVEL = "INFO"
_LOGGING_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_TRACE_LINE = re.compile(
    r"^\S+ +(?P<level>[A-Z]+) +\[(?P<source>[^\]]+)\] +(?P<kind>\S+)"
    r"(?: (?P<payload>\{.*\}))?$"
)
_EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Pièces de test injectées dans la racine de composition
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Embedder déterministe : 384 dimensions, aucun modèle, aucun réseau.

    Deux textes différents rendent des vecteurs différents, et un même texte
    rend toujours le même vecteur. Suffisant pour la mémoire SQLite réelle des
    tests ; ce n'est pas un modèle sémantique.
    """

    def __init__(self) -> None:
        self.embedded: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embedded.append(text)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [
            ((digest[index % len(digest)] / 255.0) - 0.5) * 2.0
            for index in range(_EMBEDDING_DIM)
        ]

    async def warmup(self) -> None:
        return None


class ManualClock:
    """Horloge virtuelle : les minuteries ne partent qu'à ``advance``.

    Le serveur ne distingue pas cette horloge de ``AsyncioClock`` : elle expose
    la même couture (``schedule``, ``cancel``, ``recv``, ``close``), et elle est
    aussi la source de temps de l'authentification. Avancer le temps dépose les
    ``TimerFired`` dus, dans l'ordre de leurs échéances.
    """

    def __init__(self, now: float = 0.0) -> None:
        self._now = now
        self._timers: dict[str, tuple[float, int, float]] = {}
        self._sequence = 0
        self._events: asyncio.Queue[TimerFired] = asyncio.Queue()

    @property
    def now(self) -> float:
        return self._now

    def __call__(self) -> float:
        """L'horloge murale du serveur (auth) suit le même temps virtuel."""
        return self._now

    def schedule(self, connection_id: str, timeout: float) -> None:
        self.cancel(connection_id)
        self._sequence += 1
        self._timers[connection_id] = (
            self._now + timeout,
            self._sequence,
            timeout,
        )

    def cancel(self, connection_id: str) -> None:
        self._timers.pop(connection_id, None)

    async def recv(self) -> TimerFired:
        return await self._events.get()

    def close(self) -> None:
        self._timers.clear()

    @property
    def scheduled(self) -> Mapping[str, float]:
        """Délais armés, par identifiant de connexion."""
        return {key: timeout for key, (_, _, timeout) in self._timers.items()}

    @property
    def pending(self) -> Mapping[str, float]:
        """Temps restant avant chaque échéance, par connexion."""
        return {
            key: due - self._now
            for key, (due, _, _) in self._timers.items()
        }

    def next_timeout(self) -> float:
        """Temps restant avant la prochaine échéance armée."""
        if not self._timers:
            raise AssertionError("aucune minuterie armée")
        return min(due - self._now for due, _, _ in self._timers.values())

    def advance(self, seconds: float) -> list[str]:
        """Avance le temps virtuel et dépose les minuteries devenues dues."""
        self._now += seconds
        due = sorted(
            (
                (due_at, sequence, connection_id)
                for connection_id, (due_at, sequence, _) in self._timers.items()
                if due_at <= self._now
            )
        )
        fired: list[str] = []
        for _, _, connection_id in due:
            self._timers.pop(connection_id, None)
            fired.append(connection_id)
            self._events.put_nowait(TimerFired(connection_id=connection_id))
        return fired


@dataclass(frozen=True)
class _Turn:
    """Un tour de modèle scripté : réponses à livrer et délai avant livraison."""

    responses: tuple[object, ...]
    delay: float | None


#: Signal ``go_away`` d'une Connexion Live qui se termine (défaillance).
_GO_AWAY = SimpleNamespace(time_left="0s")


class FakeLiveSession:
    """Session Live de test : reçoit les entrées, rend les tours scriptés.

    Elle expose la même surface que la session ``google.genai`` utilisée par
    ``GeminiLiveAdapter`` : ``receive()`` rend **un** tour de modèle puis se
    termine, et l'adaptateur rappelle ``receive()`` pour le tour suivant.
    """

    def __init__(self, model: FakeLiveModel) -> None:
        self._model = model
        self._turns: asyncio.Queue[tuple[object, ...] | None] = asyncio.Queue()
        self.closed = False

    async def send_realtime_input(self, **kwargs: Any) -> None:
        blob = kwargs.get("audio")
        data = getattr(blob, "data", None)
        if data:
            self._model.received_audio.append(bytes(data))
            await self._model._trigger(self)

    async def send_client_content(
        self, *, turns: object, turn_complete: bool = False
    ) -> None:
        text = _turn_text(turns)
        if text:
            self._model.received_texts.append(text)
        if turn_complete:
            await self._model._trigger(self)

    async def send_tool_response(self, *, function_responses: object) -> None:
        response = getattr(function_responses, "response", None) or {}
        self._model.tool_results.append(
            {
                "id": getattr(function_responses, "id", ""),
                "name": getattr(function_responses, "name", ""),
                "response": dict(response),
            }
        )
        await self._model._trigger(self)

    async def receive(self) -> AsyncIterator[object]:
        turn = await self._turns.get()
        if turn is None:
            return
        for response in turn:
            yield response
        return

    def close(self) -> None:
        self.closed = True
        self._turns.put_nowait(None)


class FakeLiveContext:
    """Contexte ``live_connect`` : la session de test, ouverte et refermée."""

    def __init__(self, session: FakeLiveSession) -> None:
        self._session = session

    async def __aenter__(self) -> FakeLiveSession:
        return self._session

    async def __aexit__(self, *exc: Any) -> bool:
        self._session.close()
        return False


class FakeLiveModel:
    """Modèle Live de test, injecté à la couture ``live_connect``.

    Ce n'est pas un mock interne du Coordinator : l'adaptateur Gemini réel
    reste en place, avec ses traductions et ses traces. Seul le transport vers
    Google est remplacé, exactement comme le font les tests d'intégration
    existants.
    """

    def __init__(self) -> None:
        #: Délai avant chaque tour scripté (secondes réelles).
        self.delay = 0.0
        self.received_audio: list[bytes] = []
        self.received_texts: list[str] = []
        self.tool_results: list[dict[str, Any]] = []
        self.configs: list[Any] = []
        self.sessions: list[FakeLiveSession] = []
        self._turns: asyncio.Queue[_Turn] = asyncio.Queue()
        self._deliveries: set[asyncio.Task[None]] = set()
        self._next_tool_id = 0

    # --- script du modèle ---------------------------------------------------

    def reply(
        self,
        text: str = "",
        audio: bytes | None = None,
        *,
        heard: str = "",
        delay: float | None = None,
    ) -> None:
        """Scripte un tour de modèle : texte transcrit et/ou audio descendant.

        ``heard`` simule ce que le modèle a entendu de l'utilisateur : la
        transcription d'entrée traverse le noyau comme une vraie prise de parole.
        """
        server_content = None
        if text or heard:
            server_content = SimpleNamespace(
                input_transcription=(
                    SimpleNamespace(text=heard) if heard else None
                ),
                output_transcription=(
                    SimpleNamespace(text=text) if text else None
                ),
            )
        self._turns.put_nowait(
            _Turn(
                responses=(
                    SimpleNamespace(data=audio, server_content=server_content),
                ),
                delay=delay,
            )
        )

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
        *,
        tool_id: str | None = None,
    ) -> str:
        """Scripte un appel de Tool ; rend l'identifiant utilisé."""
        self._next_tool_id += 1
        identifier = tool_id or f"tool-{self._next_tool_id}"
        call = SimpleNamespace(
            id=identifier, name=name, args=dict(arguments or {})
        )
        self._turns.put_nowait(
            _Turn(
                responses=(
                    SimpleNamespace(
                        tool_call=SimpleNamespace(function_calls=[call])
                    ),
                ),
                delay=None,
            )
        )
        return identifier

    def fail(self, *, delay: float | None = None) -> None:
        """Scripte une défaillance du modèle (``go_away``) au prochain tour.

        C'est le chemin d'échec d'une Connexion Live : le noyau doit le rendre
        lisible à la surface (``error``) au lieu de laisser le flux ouvert.
        """
        self._turns.put_nowait(
            _Turn(
                responses=(SimpleNamespace(go_away=_GO_AWAY),),
                delay=delay,
            )
        )

    # --- observation --------------------------------------------------------

    @property
    def session(self) -> FakeLiveSession:
        if not self.sessions:
            raise AssertionError("aucune Connexion Live ouverte")
        return self.sessions[-1]

    def declarations(self) -> list[Any]:
        """Déclarations de Tools de la dernière Connexion Live."""
        tools = self.configs[-1].tools or []
        return [decl for tool in tools for decl in (tool.function_declarations or [])]

    def instruction(self) -> str:
        """Instruction système de la dernière Connexion Live."""
        content = self.configs[-1].system_instruction
        if content is None:
            return ""
        return "\n".join(part.text or "" for part in content.parts)

    # --- couture ``live_connect`` ------------------------------------------

    def __call__(self, model: str, config: Any) -> FakeLiveContext:
        self.configs.append(config)
        session = FakeLiveSession(self)
        self.sessions.append(session)
        return FakeLiveContext(session)

    async def _trigger(self, session: FakeLiveSession) -> None:
        """Livre le prochain tour scripté à l'entrée qui vient d'arriver."""
        if self._turns.empty():
            return
        turn = self._turns.get_nowait()
        delay = self.delay if turn.delay is None else turn.delay

        async def deliver() -> None:
            if delay:
                await asyncio.sleep(delay)
            if not session.closed:
                session._turns.put_nowait(turn.responses)

        task = asyncio.create_task(deliver())
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    async def aclose(self) -> None:
        """Annule les tours encore en attente et ferme les sessions."""
        for task in list(self._deliveries):
            task.cancel()
        if self._deliveries:
            await asyncio.gather(*self._deliveries, return_exceptions=True)
        for session in self.sessions:
            session.close()


def _turn_text(turns: object) -> str:
    """Texte d'un ``types.Content`` envoyé par l'adaptateur, sans dépendre de lui."""
    parts = getattr(turns, "parts", None) or []
    return "".join(getattr(part, "text", "") or "" for part in parts)


class FakeSecretValidator:
    """Validateur de Secrets de test : aucun réseau, refus explicites.

    Il accepte tout sauf les valeurs marquées ``reject`` et peut simuler un
    service injoignable. La valeur reçue est conservée pour que les tests
    puissent prouver qu'aucune réponse ne la reprend, jamais pour l'afficher.
    """

    def __init__(self, *, reject: tuple[str, ...] = ()) -> None:
        self.rejected = set(reject)
        self.unavailable = False
        self.seen: list[tuple[str, str]] = []

    async def validate(self, name: str, value: str) -> str | None:
        self.seen.append((name, value))
        if self.unavailable:
            return "Le service Gemini est injoignable pour le moment."
        if value in self.rejected:
            return "Cette clé Gemini est refusée par le service."
        return None


# ---------------------------------------------------------------------------
# Plugins de test : dossiers déposés avant le démarrage
# ---------------------------------------------------------------------------

#: Code du plugin de fixture : un Tool ``<nom>__echo`` et une instruction.
#: Il ne lit ni config ni secret, sauf quand le test le demande.
PLUGIN_CODE = '''
from plugin_api import PluginBinding, ToolBinding, ToolSpec


async def handle(arguments):
    return {"status": "ok", "plugin": "fixture"}


def create(context):
    return PluginBinding(
        name=context.name,
        tools=(
            ToolBinding(
                spec=ToolSpec(
                    name=f"{context.name}__echo",
                    description="répond à tout",
                    parameters={"type": "object", "properties": {}},
                ),
                handler=handle,
            ),
        ),
        instruction=f"Utilise {context.name}__echo pour répéter.",
    )
'''

#: Code d'un plugin qui n'accepte de démarrer que si son secret vaut la valeur
#: attendue : la seule façon de prouver la précédence sans exposer la valeur.
SECRET_CHECKING_CODE = '''
from plugin_api import PluginBinding, PluginUnavailable, ToolBinding, ToolSpec

EXPECTED = "{expected}"


async def handle(arguments):
    return {{"status": "ok"}}


def create(context):
    if context.secrets.get("token") != EXPECTED:
        raise PluginUnavailable("secret absent ou différent")
    return PluginBinding(
        name=context.name,
        tools=(
            ToolBinding(
                spec=ToolSpec(
                    name=f"{{context.name}}__echo",
                    description="répond à tout",
                    parameters={{"type": "object", "properties": {{}}}},
                ),
                handler=handle,
            ),
        ),
    )
'''


def plugin_manifest(
    name: str,
    *,
    version: str = "0.1.0",
    description: str = "Plugin de test",
    platform: str = "server",
    entrypoint: str = "plugin.py",
    config: list[dict[str, object]] | None = None,
    secrets: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Manifeste minimal valide, à surcharger champ par champ."""
    manifest: dict[str, object] = {
        "api_version": 1,
        "name": name,
        "version": version,
        "description": description,
        "platform": platform,
        "entrypoint": entrypoint,
    }
    if config is not None:
        manifest["config"] = config
    if secrets is not None:
        manifest["secrets"] = secrets
    return manifest


def write_plugin(
    plugins_dir: Path,
    name: str,
    *,
    manifest: Mapping[str, object] | None = None,
    code: str = PLUGIN_CODE,
    entrypoint: str = "plugin.py",
) -> Path:
    """Dépose un dossier de Plugin complet, prêt pour un démarrage."""
    directory = plugins_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    payload = plugin_manifest(name) if manifest is None else manifest
    (directory / "plugin.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (directory / entrypoint).write_text(code, encoding="utf-8")
    return directory


def write_plugins_state(
    path: Path,
    *,
    enabled: tuple[str, ...] = (),
    config: Mapping[str, Mapping[str, object]] | None = None,
    extra: Mapping[str, object] | None = None,
) -> Path:
    """Écrit un fichier d'état, avec des clés inconnues si demandé."""
    payload: dict[str, object] = {
        "enabled": list(enabled),
        "config": {name: dict(values) for name, values in (config or {}).items()},
    }
    if extra:
        payload.update(extra)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


# ---------------------------------------------------------------------------
# Surfaces du serveur démarré
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerPaths:
    """Tout l'état du serveur, sous le répertoire temporaire du test."""

    root: Path
    memory_db: Path
    auth_db: Path
    settings_db: Path
    master_key: Path
    plugins_dir: Path
    plugins_state: Path
    log_dir: Path
    log_file: Path
    static_dir: Path


class ConnectedClient:
    """Client du Protocole partagé, connecté au vrai transport avec un Jeton."""

    def __init__(
        self,
        server: AthenaServer,
        ws: Any,
        registered: Registered,
        inbox: asyncio.Queue[object],
        pump: asyncio.Task[None],
    ) -> None:
        self._server = server
        self._ws = ws
        self.registered = registered
        self._inbox = inbox
        self._pump = pump

    @property
    def client_id(self) -> str:
        return self.registered.client_id

    async def send(self, message: object) -> None:
        await self._ws.send(encode(message))

    async def next_message(self, timeout: float = 1.0) -> object:
        """Le prochain message du serveur, décodé (``Audio``, ``SessionEnded``…)."""
        return await asyncio.wait_for(self._inbox.get(), timeout)

    def queued_messages(self) -> bool:
        """Vrai si un message est déjà arrivé et attend d'être lu."""
        return not self._inbox.empty()

    async def start_session(self) -> None:
        """Ouvre une Session et attend que le noyau soit actif.

        ``SessionStarted`` n'est pas un acquittement de la demande : le serveur
        le livre avec le premier audio du modèle. Ici on attend donc la minuterie
        d'inactivité, armée quand la Connexion Live est ouverte (``LlmOpened``),
        pour que l'audio envoyé juste après ne soit jamais perdu.
        """
        await self.send(SessionStart())
        await self._server.wait_until(
            lambda: bool(self._server.clock.scheduled), timeout=1.0
        )

    async def send_audio(self, data: bytes) -> None:
        await self.send(Audio(data=data))

    async def wait_message(
        self, message_type: type, timeout: float = 1.0
    ) -> Any:
        """Lit jusqu'à un message du type demandé (les autres sont ignorés)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(
                    f"aucun message {message_type.__name__} en {timeout}s"
                )
            message = await self.next_message(timeout=remaining)
            if isinstance(message, message_type):
                return message

    async def close(self) -> None:
        await self._ws.close()
        if not self._pump.done():
            self._pump.cancel()
            await asyncio.gather(self._pump, return_exceptions=True)


@dataclass(frozen=True)
class SseEvent:
    """Un événement ``text/event-stream`` : nom d'événement et données brutes."""

    event: str
    data: str

    def json(self) -> Any:
        """Les données décodées, quand le flux porte du JSON."""
        return json.loads(self.data) if self.data else None


async def read_sse(response: aiohttp.ClientResponse) -> AsyncIterator[SseEvent]:
    """Lit un flux ``text/event-stream`` et rend ses événements.

    C'est le lecteur des Tours texte de la Console (``POST /api/chat``, ticket
    #172) : le harnais le fournit dès maintenant pour que ces tests n'aient pas
    à réécrire l'analyse du flux. Les lignes ``data:`` multiples d'un même
    événement sont recollées par un saut de ligne, les commentaires ``:`` sont
    ignorés, et un événement sans ``data`` n'est pas rendu.
    """
    event = "message"
    data: list[str] = []
    async for raw in response.content:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield SseEvent(event=event, data="\n".join(data))
            event = "message"
            data = []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:") :].lstrip())
    if data:
        yield SseEvent(event=event, data="\n".join(data))


class TraceStream:
    """Abonnement au flux d'observation de la Console (``GET /api/stream``)."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, hello: dict) -> None:
        self._ws = ws
        self.hello = hello
        self.backlog: list[dict] = list(hello["data"].get("backlog", []))

    async def next_frame(self, timeout: float = 1.0) -> dict:
        """La prochaine trame brute (``trace`` ou ``status``)."""
        return await asyncio.wait_for(self._ws.receive_json(), timeout)

    async def next_trace(
        self,
        kind: str | None = None,
        *,
        source: str | None = None,
        timeout: float = 1.0,
    ) -> dict:
        """La prochaine trame ``trace`` qui correspond au filtre demandé."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise AssertionError(
                    f"aucune Trace {kind or '*'} en {timeout}s"
                )
            frame = await self.next_frame(timeout=remaining)
            if frame.get("type") != "trace":
                continue
            trace = frame["data"]
            if kind is not None and trace["kind"] != kind:
                continue
            if source is not None and trace["source"] != source:
                continue
            return trace

    def find(self, kind: str, *, source: str | None = None) -> dict | None:
        """Cherche une Trace dans le backlog déjà reçu (``None`` sinon)."""
        for trace in reversed(self.backlog):
            if trace["kind"] != kind:
                continue
            if source is not None and trace["source"] != source:
                continue
            return trace
        return None

    async def close(self) -> None:
        await self._ws.close()


class AthenaServer:
    """Le serveur démarré et ses moyens de le piloter (voir la doc du module)."""

    def __init__(
        self,
        *,
        app: App,
        runner: asyncio.Task[None],
        model: FakeLiveModel,
        clock: ManualClock,
        validator: FakeSecretValidator,
        paths: ServerPaths,
        console_port: int,
        transport_port: int,
        webhook_port: int | None,
        saved_logging: tuple[list[logging.Handler], int],
    ) -> None:
        self.app = app
        self.model = model
        self.clock = clock
        self.validator = validator
        self.paths = paths
        self._runner = runner
        self._console_port = console_port
        self._transport_port = transport_port
        self._webhook_port = webhook_port
        self._saved_logging = saved_logging
        self._session: aiohttp.ClientSession | None = None
        self._client_token: str | None = None
        self._stopped = False
        #: Corps de la réponse d'Installation (dont la clé maître, une fois).
        self.setup_response: dict[str, Any] = {}

    # --- adresses -----------------------------------------------------------

    @property
    def root(self) -> Path:
        return self.paths.root

    @property
    def url(self) -> str:
        """Base HTTP de la Console (``http://127.0.0.1:<port éphémère>``)."""
        return f"http://127.0.0.1:{self._console_port}"

    @property
    def transport_url(self) -> str:
        """URL WebSocket du transport audio."""
        return f"ws://127.0.0.1:{self._transport_port}"

    @property
    def webhook_port(self) -> int:
        """Port du Webhook, quand la surface a été demandée."""
        if self._webhook_port is None:
            raise AssertionError("le Webhook n'est pas activé dans ce serveur")
        return self._webhook_port

    # --- clients HTTP -------------------------------------------------------

    def anonymous(self) -> aiohttp.ClientSession:
        """Client HTTP anonyme : ni cookie, ni ``Origin`` (sonde, refus)."""
        return aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())

    def console(self) -> aiohttp.ClientSession:
        """Client HTTP de navigateur : cookie ``Secure`` accepté et ``Origin``."""
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(
                unsafe=True, treat_as_secure_origin=self.url
            ),
            headers={"Origin": self.url},
        )

    async def install(
        self,
        *,
        username: str = "admin",
        password: str = TEST_PASSWORD,
    ) -> aiohttp.ClientSession:
        """Crée l'unique Compte par la vraie route et garde le Ticket Console.

        Aucun code n'est demandé (ADR-0025) : le premier visiteur devient
        propriétaire. La réponse d'Installation porte la clé maître une seule
        fois ; le harnais la laisse dans la réponse, que les tests peuvent lire.
        """
        http = self.console()
        async with http.post(
            f"{self.url}/api/setup",
            json={"username": username, "password": password},
        ) as response:
            assert response.status == 200, await response.text()
            self.setup_response = await response.json()
        self._session = http
        return http

    @property
    def http(self) -> aiohttp.ClientSession:
        """Le client HTTP authentifié rendu par ``install()``."""
        if self._session is None:
            raise AssertionError(
                "appelez d'abord `await server.install()` pour obtenir un Ticket"
            )
        return self._session

    async def login(
        self,
        *,
        username: str = "admin",
        password: str = TEST_PASSWORD,
    ) -> aiohttp.ClientSession:
        """Ouvre un Ticket sur une instance déjà installée (second démarrage)."""
        http = self.console()
        async with http.post(
            f"{self.url}/api/login",
            json={"username": username, "password": password},
        ) as response:
            assert response.status == 200, await response.text()
            await response.json()
        self._session = http
        return http

    def plugins_state(self) -> dict[str, Any]:
        """Le fichier d'état des Plugins, déjà décodé (vide s'il est absent)."""
        path = self.paths.plugins_state
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    async def access_token(
        self,
        *,
        name: str = "desktop",
        kind: str = "client",
        source: str | None = None,
    ) -> str:
        """Crée un Jeton d'accès par l'API de la Console et rend son secret."""
        if kind == "client" and source is None and self._client_token is not None:
            if name == "desktop":
                return self._client_token
        payload: dict[str, object] = {"name": name, "kind": kind}
        if source is not None:
            payload["source"] = source
        async with self.http.post(
            f"{self.url}{TOKENS_PATH}", json=payload
        ) as response:
            assert response.status == 201, await response.text()
            body = await response.json()
        secret: str = body["secret"]
        if kind == "client" and source is None and name == "desktop":
            self._client_token = secret
        return secret

    # --- transport ----------------------------------------------------------

    @asynccontextmanager
    async def client(
        self,
        *,
        name: str = "desktop",
        platform: str = "linux",
        token: str | None = None,
    ) -> AsyncIterator[ConnectedClient]:
        """Ouvre une connexion de Client au transport, déjà enregistrée."""
        secret = token if token is not None else await self.access_token(name=name)
        ws = await _connect_when_ready(
            self.transport_url,
            additional_headers={"Authorization": f"Bearer {secret}"},
        )
        inbox: asyncio.Queue[object] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for raw in ws:
                    inbox.put_nowait(decode(raw))
            except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
                return

        task: asyncio.Task[None] | None = None
        try:
            await ws.send(
                encode(Register(client_name=name, platform=platform))
            )
            registered = decode(await asyncio.wait_for(ws.recv(), 1.0))
            assert isinstance(registered, Registered), registered
            # Le pompage démarre après l'acquittement : une seule coroutine lit
            # le socket, jamais deux.
            task = asyncio.create_task(pump())
            yield ConnectedClient(self, ws, registered, inbox, task)
        finally:
            await ws.close()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    # --- traces et journaux ------------------------------------------------

    @asynccontextmanager
    async def trace_stream(self) -> AsyncIterator[TraceStream]:
        """S'abonne au WebSocket ``/api/stream`` de la Console (Ticket exigé)."""
        ws = await _ws_connect_when_ready(
            self.http, f"{self.url}{STREAM_PATH}"
        )
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 2.0)
            assert hello.get("type") == "hello", hello
            yield TraceStream(ws, hello)
        finally:
            await ws.close()

    def trace_log(self) -> list[dict[str, Any]]:
        """Le journal ``logs/server.log``, déjà analysé en entrées de Trace."""
        if not self.paths.log_file.is_file():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.paths.log_file.read_text(encoding="utf-8").splitlines():
            match = _TRACE_LINE.match(line)
            if match is None:
                continue
            payload = match.group("payload")
            entries.append(
                {
                    "timestamp": line.split(" ", 1)[0],
                    "level": match.group("level").lower(),
                    "source": match.group("source"),
                    "kind": match.group("kind"),
                    "payload": json.loads(payload) if payload else {},
                }
            )
        return entries

    def read_log(self) -> str:
        """Le journal brut, pour les assertions qui portent sur le texte."""
        if not self.paths.log_file.is_file():
            return ""
        return self.paths.log_file.read_text(encoding="utf-8")

    def emit_log(self, logger: str, message: str, *, level: str = "info") -> None:
        """Émet une entrée de logging dans le flux unique (trace ``logger``)."""
        logging.getLogger(logger).log(
            _LOGGING_LEVELS[level], message
        )

    # --- instantané ---------------------------------------------------------

    async def status(self) -> dict[str, Any]:
        """``GET /api/status``, Ticket Console compris (instantané étendu)."""
        async with self.http.get(f"{self.url}/api/status") as response:
            assert response.status == 200, await response.text()
            return await response.json()

    # --- bases --------------------------------------------------------------

    def query(
        self,
        db_path: Path,
        sql: str,
        params: tuple[object, ...] = (),
    ) -> list[dict[str, Any]]:
        """Lit une base du serveur (mémoire ou auth) et rend des lignes-dicts."""
        connection = sqlite3.connect(db_path)
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, params)]
        finally:
            connection.close()

    # --- attentes -----------------------------------------------------------

    async def wait_until(
        self, predicate: Callable[[], bool], timeout: float = 1.0
    ) -> None:
        """Attend qu'une condition synchrone devienne vraie."""
        await wait_until(predicate, timeout)

    async def wait_stopped(self, timeout: float = 2.0) -> None:
        """Attend la fin de la boucle du serveur (sortie propre).

        L'application des Réglages redémarre le processus en demandant l'arrêt
        du serveur : un test peut observer cette sortie sans tuer pytest, le
        serveur n'étant qu'une tâche de la boucle du test.
        """
        await asyncio.wait_for(asyncio.shield(self._runner), timeout)

    # --- cycle de vie -------------------------------------------------------

    async def stop(self) -> None:
        """Arrête proprement : Événement d'arrêt, adaptateurs, logging."""
        if self._stopped:
            return
        self._stopped = True
        self.app.request_shutdown()
        try:
            await asyncio.wait_for(self._runner, 5.0)
        finally:
            await self.model.aclose()
            if self._session is not None:
                await self._session.close()
                self._session = None
            handlers, level = self._saved_logging
            root = logging.getLogger()
            root.handlers[:] = handlers
            root.setLevel(level)


# ---------------------------------------------------------------------------
# Démarrage
# ---------------------------------------------------------------------------


def _free_ports(count: int) -> list[int]:
    """``count`` ports libres **distincts** réservés par l'OS, jamais des ports fixes.

    Les sockets restent ouverts pendant la réservation : deux appels séparés
    pourraient rendre deux fois le même port éphémère, et les deux Surfaces du
    serveur entreraient alors en collision.
    """
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [int(sock.getsockname()[1]) for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def _paths(root: Path, static_dir: Path | None) -> ServerPaths:
    log_dir = root / "logs"
    return ServerPaths(
        root=root,
        memory_db=root / "memory.db",
        auth_db=root / "auth.db",
        settings_db=root / "settings.db",
        master_key=root / "master.key",
        plugins_dir=root / "plugins",
        plugins_state=root / "plugins.json",
        log_dir=log_dir,
        log_file=log_dir / "server.log",
        static_dir=static_dir if static_dir is not None else root / "dist",
    )


def _config(
    paths: ServerPaths,
    *,
    console_port: int,
    transport_port: int,
    webhook_port: int | None,
    gemini_key: str,
    restart_on_apply: bool = False,
    restart_command: str = "docker restart athena",
) -> AppConfig:
    return AppConfig(
        gemini=GeminiConfig(api_key=gemini_key, model="gemini-live-test"),
        server=ServerConfig(host="127.0.0.1", port=transport_port),
        console=ConsoleConfig(
            enabled=True,
            host="127.0.0.1",
            port=console_port,
            static_dir=str(paths.static_dir),
        ),
        webhook=WebhookConfig(
            enabled=webhook_port is not None,
            host="127.0.0.1",
            port=webhook_port or 1,
        ),
        auth=AuthConfig(db_path=str(paths.auth_db)),
        memory=MemoryConfig(db_path=str(paths.memory_db)),
        plugins=PluginsConfig(
            directory=str(paths.plugins_dir),
            state_path=str(paths.plugins_state),
        ),
        settings=SettingsConfig(
            db_path=str(paths.settings_db),
            master_key_file=str(paths.master_key),
        ),
        log_dir=str(paths.log_dir),
        log_level=_LOG_LEVEL,
        restart_on_apply=restart_on_apply,
        restart_command=restart_command,
    )


async def _connect_when_ready(
    url: str, *, additional_headers: Mapping[str, str] | None = None
) -> Any:
    """Attend que le transport écoute, puis ouvre la connexion WebSocket."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while True:
        try:
            return await websockets.connect(
                url, additional_headers=additional_headers
            )
        except OSError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.01)


async def _ws_connect_when_ready(
    http: aiohttp.ClientSession, url: str
) -> aiohttp.ClientWebSocketResponse:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while True:
        try:
            return await http.ws_connect(url)
        except aiohttp.ClientConnectorError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.01)


async def _wait_until_ready(http: aiohttp.ClientSession, url: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while True:
        try:
            async with http.get(f"{url}{HEALTH_PATH}") as response:
                if response.status == 200:
                    return
        except aiohttp.ClientConnectorError:
            pass
        if loop.time() > deadline:
            raise AssertionError("la Console n'a pas démarré")
        await asyncio.sleep(0.01)


@asynccontextmanager
async def running_server(
    root: Path,
    *,
    static_dir: Path | None = None,
    prepare: Callable[[ServerPaths], None] | None = None,
    gemini_key: str = "test-key",
    env: Mapping[str, str] | None = None,
    secrets: SecretResolver | None = None,
    settings: SecretStore | None = None,
    webhook: bool = False,
    secret_validator: SecretValidator | None = None,
    restart_on_apply: bool = False,
    restart_command: str = "docker restart athena",
    restart: Callable[[], None] | None = None,
) -> AsyncIterator[AthenaServer]:
    """Démarre le serveur complet sur des ports éphémères et l'arrête à la sortie.

    ``root`` isole tout l'état du test : bases mémoire, auth et réglages,
    plugins, journaux, build statique et clé maître. ``prepare`` s'exécute avant
    le démarrage, avec les chemins déjà calculés : c'est là qu'un test dépose un
    plugin, un fichier d'état ou un build. Aucun port n'est fixe, aucun secret
    réel n'est lu, et l'ordre des tests n'a aucune importance.

    Les coutures ``env``, ``secrets``, ``settings`` et ``secret_validator``
    remplacent la configuration effective **sans relancer** de processus : un
    test peut ainsi prouver la précédence de l'environnement ou jouer une clé
    maître perdue.
    """
    root.mkdir(parents=True, exist_ok=True)
    paths = _paths(root, static_dir)
    if prepare is not None:
        prepare(paths)
    ports = _free_ports(3 if webhook else 2)
    console_port, transport_port = ports[0], ports[1]
    webhook_port = ports[2] if webhook else None

    model = FakeLiveModel()
    clock = ManualClock()
    validator = FakeSecretValidator()
    root_logger = logging.getLogger()
    saved_logging = (root_logger.handlers[:], root_logger.level)

    app = build_app(
        # La configuration programmatique est d'abord recouverte par
        # l'environnement injecté : un test qui masque un réglage observe la
        # même valeur que le processus aurait chargée, jamais une divergence
        # entre le GET et le comportement.
        overlay_environment(
            _config(
                paths,
                console_port=console_port,
                transport_port=transport_port,
                webhook_port=webhook_port,
                gemini_key=gemini_key,
                restart_on_apply=restart_on_apply,
                restart_command=restart_command,
            ),
            {} if env is None else env,
        ),
        secrets=secrets,
        settings=settings,
        secret_validator=secret_validator or validator,
        env={} if env is None else env,
        env_file=None,
        live_connect=model,
        clock=clock,
        embedder=FakeEmbedder(),
        auth_now=clock,
        monotonic=clock,
        restart=restart,
    )
    runner = asyncio.create_task(app.run())
    server = AthenaServer(
        app=app,
        runner=runner,
        model=model,
        clock=clock,
        validator=validator,
        paths=paths,
        console_port=console_port,
        transport_port=transport_port,
        webhook_port=webhook_port,
        saved_logging=saved_logging,
    )
    try:
        async with server.anonymous() as probe:
            await _wait_until_ready(probe, server.url)
        yield server
    finally:
        await server.stop()
