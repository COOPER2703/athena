from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Sequence

import pytest
import websockets

from conftest import FakeJudge, RecordingTraceSink, _url, register, wait_until
from protocol import SessionStart, encode
from server.adapters.gemini import GeminiLiveAdapter
from server.adapters.plugins import PluginLoader
from server.adapters.plugins.loader import PluginLoadError
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.root import App
from server.app.secrets import EnvSecretResolver
from server.app.trace import FanOutTraceSink, FileTraceSink
from server.core.plugins import PluginContext, PluginRegistry
from tavily_stub import TavilyStub

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "websearch"
SECRET = "tvly-long-lived-test-key"
SECRET_VARIABLE = "ATHENA_PLUGIN_WEBSEARCH_API_KEY"


def test_manifest_declares_only_static_identity_and_requirements() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    assert manifest == {
        "api_version": 1,
        "name": "websearch",
        "version": "0.1.0",
        "description": "Recherche web via le moteur Tavily",
        "platform": "server",
        "entrypoint": "plugin.py",
        "config": [
            {"key": "base_url", "type": "url"},
            {"key": "search_depth", "type": "string"},
            {"key": "language", "type": "string"},
            {"key": "country", "type": "string"},
            {"key": "strict_language", "type": "string"},
        ],
        "secrets": [{"key": "api_key", "required": True}],
    }


def test_absent_api_key_fails_the_start_and_names_the_variable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "plugins.json"
    state_path.write_text(json.dumps({"enabled": ["websearch"]}), encoding="utf-8")
    loader = PluginLoader(
        directory=REPO_ROOT / "plugins",
        state_path=state_path,
        secrets=EnvSecretResolver({}),
        trace_sink=RecordingTraceSink(),
    )

    with pytest.raises(PluginLoadError) as excinfo:
        asyncio.run(
            loader.load(PluginRegistry(), push_event=lambda source, text: None)
        )

    assert SECRET_VARIABLE in str(excinfo.value)


# ---------------------------------------------------------------------------
# Faux Gemini et transport : mêmes formes que ``test_plugins_homeassistant``.
# ---------------------------------------------------------------------------


class _ScriptedSession:
    """Fake Live session : le test pousse les réponses, l'adaptateur les lit."""

    def __init__(self) -> None:
        self._responses: asyncio.Queue[Any] = asyncio.Queue()
        self.function_responses: list[Any] = []

    async def send_realtime_input(self, **kwargs: Any) -> None:
        pass

    async def send_client_content(self, **kwargs: Any) -> None:
        pass

    async def send_tool_response(self, function_responses: Any) -> None:
        self.function_responses.append(function_responses)

    async def receive(self) -> Any:
        yield await self._responses.get()

    def call_tool(self, tool_id: str, name: str, arguments: dict) -> None:
        call = SimpleNamespace(id=tool_id, name=name, args=arguments)
        response = SimpleNamespace(tool_call=SimpleNamespace(function_calls=[call]))
        self._responses.put_nowait(response)


class _ScriptedContext:
    def __init__(self, session: _ScriptedSession) -> None:
        self._session = session

    async def __aenter__(self) -> _ScriptedSession:
        return self._session

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _ToolLiveConnect:
    """Fake ``live_connect`` : retient les configs et les sessions ouvertes."""

    def __init__(self) -> None:
        self.configs: list[Any] = []
        self.sessions: list[_ScriptedSession] = []

    def __call__(self, model: str, config: Any) -> _ScriptedContext:
        session = _ScriptedSession()
        self.configs.append(config)
        self.sessions.append(session)
        return _ScriptedContext(session)

    @property
    def session(self) -> _ScriptedSession:
        return self.sessions[-1]

    def declarations(self) -> list[Any]:
        tools = self.configs[-1].tools or []
        return [decl for tool in tools for decl in (tool.function_declarations or [])]

    def instruction(self) -> str:
        content = self.configs[-1].system_instruction
        if content is None:
            return ""
        return "\n".join(part.text or "" for part in content.parts)


@contextlib.asynccontextmanager
async def _running_app(
    *,
    registry: PluginRegistry | None = None,
    loader: PluginLoader | None = None,
    retry_interval: float = 5.0,
    announce_prompt: str = "",
    judge: Any = None,
    extra_sinks: Sequence[Any] = (),
) -> AsyncIterator[
    tuple[Any, _ToolLiveConnect, WebSocketTransport, RecordingTraceSink]
]:
    sink = RecordingTraceSink()
    trace_sink = FanOutTraceSink(sink, *extra_sinks) if extra_sinks else sink
    transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=trace_sink)
    live = _ToolLiveConnect()
    gemini = GeminiLiveAdapter(
        api_key="test-key",
        model="gemini-live",
        voice="Aoede",
        live_connect=live,
        trace_sink=trace_sink,
    )
    app = App(
        transport=transport,
        gemini=gemini,
        clock=AsyncioClock(),
        trace_sink=trace_sink,
        announce_prompt=announce_prompt,
        judge=judge,
        plugins=registry,
        plugin_loader=loader,
        plugin_retry_interval=retry_interval,
    )
    runner = asyncio.create_task(app.run())
    try:
        await wait_until(lambda: bool(sink.of_kind("started")))
        yield app, live, transport, sink
    finally:
        app.request_shutdown()
        await asyncio.wait_for(runner, 1.0)


@contextlib.asynccontextmanager
async def _live_session(
    transport: WebSocketTransport, live: _ToolLiveConnect
) -> AsyncIterator[_ScriptedSession]:
    async with websockets.connect(_url(transport)) as ws:
        await register(ws, transport, drain=False)
        await ws.send(encode(SessionStart()))
        await wait_until(lambda: bool(live.sessions))
        yield live.session


# ---------------------------------------------------------------------------
# Installation du vrai plugin dans un répertoire temporaire.
# ---------------------------------------------------------------------------


def _install_websearch(
    tmp_path: Path,
    base_url: str,
    *,
    config: dict[str, str] | None = None,
    search_timeout: float | None = None,
    cache_ttl: float | None = None,
) -> tuple[Path, Path]:
    """Copie le vrai plugin et écrit un fichier d'état qui l'active."""
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "websearch"
    shutil.copytree(PLUGIN_DIR, plugin_dir)
    if search_timeout is not None or cache_ttl is not None:
        source = (plugin_dir / "plugin.py").read_text(encoding="utf-8")
        if search_timeout is not None:
            source = _patched_constant(source, "SEARCH_TIMEOUT", search_timeout)
        if cache_ttl is not None:
            source = _patched_constant(source, "CACHE_TTL", cache_ttl)
        (plugin_dir / "plugin.py").write_text(source, encoding="utf-8")

    state_path = tmp_path / "plugins.json"
    state_path.write_text(
        json.dumps(
            {
                "enabled": ["websearch"],
                "config": {"websearch": {"base_url": base_url, **(config or {})}},
            }
        ),
        encoding="utf-8",
    )
    return plugins_dir, state_path


def _plugins(
    tmp_path: Path,
    sink: RecordingTraceSink,
    base_url: str,
    *,
    config: dict[str, str] | None = None,
    secret: str = SECRET,
    search_timeout: float | None = None,
    cache_ttl: float | None = None,
    extra_sinks: Sequence[Any] = (),
) -> tuple[PluginRegistry, PluginLoader]:
    plugins_dir, state_path = _install_websearch(
        tmp_path,
        base_url,
        config=config,
        search_timeout=search_timeout,
        cache_ttl=cache_ttl,
    )
    registry = PluginRegistry()
    trace_sink = FanOutTraceSink(sink, *extra_sinks) if extra_sinks else sink
    loader = PluginLoader(
        directory=plugins_dir,
        state_path=state_path,
        secrets=EnvSecretResolver({SECRET_VARIABLE: secret}),
        trace_sink=trace_sink,
    )
    return registry, loader


def _patched_constant(source: str, name: str, value: float) -> str:
    """Réduit un budget du module copié ; le fichier du dépôt n'est pas touché."""
    pattern = re.compile(rf"^{name} = [0-9.]+$", re.MULTILINE)
    patched, count = pattern.subn(f"{name} = {value!r}", source, count=1)
    assert count == 1, f"{name} not found in the plugin source"
    return patched


async def _call(session: _ScriptedSession, name: str, arguments: dict) -> dict:
    before = len(session.function_responses)
    session.call_tool(f"call-{before}", name, arguments)
    await wait_until(lambda: len(session.function_responses) > before)
    return dict(session.function_responses[-1].response)


def _declared(live: _ToolLiveConnect) -> dict[str, Any]:
    return {decl.name: decl for decl in live.declarations()}


def _states(sink: RecordingTraceSink) -> list[str]:
    return [str(entry.payload["state"]) for entry in sink.of_kind("PluginState")]


# ---------------------------------------------------------------------------
# Sonde de démarrage
# ---------------------------------------------------------------------------


def test_refused_probe_degrades_the_start_then_repairs_itself(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with TavilyStub(usage_status=401) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(
                registry=registry, loader=loader, retry_interval=0.1
            ) as (app, _live, _transport, _):
                # La clé est refusée : Athena démarre quand même, dégradée.
                assert _states(sink)[0] == "failed"
                assert set(_states(sink)) == {"failed"}
                assert app.plugins.states["websearch"] == "failed"
                assert app.plugins.tools.specs() == ()
                assert "401" in str(sink.of_kind("PluginState")[0].payload["reason"])

                # La retente de fond continue tant que le moteur refuse.
                await wait_until(
                    lambda: len(sink.of_kind("PluginState")) >= 2, timeout=2.0
                )
                assert all(state == "failed" for state in _states(sink))

                # Le moteur se répare : la retente active le plugin.
                stub.usage_status = 200
                await wait_until(
                    lambda: app.plugins.states["websearch"] == "activated",
                    timeout=2.0,
                )
                # La sonde au démarrage ne consomme aucun crédit de recherche.
                assert all(
                    request["path"] == "/usage" for request in stub.requests
                )

    asyncio.run(scenario())


def test_unreachable_engine_degrades_the_start_without_killing_the_app(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        registry, loader = _plugins(tmp_path, sink, "http://127.0.0.1:1")
        async with _running_app(registry=registry, loader=loader) as (
            app,
            _live,
            _transport,
            app_sink,
        ):
            assert _states(sink) == ["failed"]
            assert app.plugins.states["websearch"] == "failed"
            assert app_sink.of_kind("started")

    asyncio.run(scenario())


def test_remaining_credits_are_traced_without_the_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with TavilyStub(usage=42, limit=1000) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                app,
                _live,
                _transport,
                _,
            ):
                assert app.plugins.states["websearch"] == "activated"
                [credits] = sink.of_kind("CreditsRemaining")
                assert credits.payload["usage"] == 42
                assert credits.payload["limit"] == 1000
                assert credits.payload["remaining"] == 958
                assert not sink.of_kind("CreditsExhausted")
                # La clé part au moteur mais n'apparaît dans aucune Trace.
                assert set(stub.auth_tokens) == {f"Bearer {SECRET}"}
                assert all(
                    SECRET not in json.dumps(entry.payload) for entry in sink.entries
                )

    asyncio.run(scenario())


def test_exhausted_quota_is_traced_as_a_warning(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with TavilyStub(usage=1000, limit=1000) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                app,
                _live,
                _transport,
                _,
            ):
                assert app.plugins.states["websearch"] == "activated"
                [warning] = sink.of_kind("CreditsExhausted")
                assert warning.payload["remaining"] == 0
                assert SECRET not in json.dumps(warning.payload)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tool de bout en bout : App → Gemini → registre → plugin → HTTP.
# ---------------------------------------------------------------------------

TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "La question ou les mots-clés à chercher sur le web",
        },
        "topic": {
            "type": "string",
            "enum": ["general", "news", "finance"],
            "description": (
                "Le type de recherche : `general` pour une question de culture "
                "générale, `news` pour l'actualité, `finance` pour la finance"
            ),
        },
        "time_range": {
            "type": "string",
            "enum": ["day", "week", "month", "year"],
            "description": (
                "La fenêtre de fraîcheur demandée par la question : `day`, "
                "`week`, `month` ou `year`"
            ),
        },
    },
    "required": ["query"],
}

# Texte exact de la spec #101, section « Instruction de plugin ».
INSTRUCTION = (
    "`web_search` interroge le web. Utilise-le pour toute question factuelle, "
    "d'actualité ou de culture générale dont tu n'es pas sûre, et jamais pour "
    "une question sur l'utilisateur ou sa maison.\n"
    "Avant d'appeler, dis en une phrase courte que tu cherches. Un appel suffit : "
    "ne relance pas la même question reformulée.\n"
    "Quand tu réponds, attribue à la source par son nom ; ne lis jamais une URL "
    "à haute voix et ne récite pas d'extrait brut.\n"
    "Si les sources se contredisent, dis-le au lieu de trancher. Si la recherche "
    "échoue, dis-le simplement et propose de réessayer."
)


def test_web_search_is_declared_and_returns_sourced_excerpts(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                assert app.plugins.states["websearch"] == "activated"
                # Le Tool est visible dans l'instantané de lecture de la Console.
                assert app.snapshot()["plugins"] == [
                    {
                        "name": "websearch",
                        "state": "activated",
                        "tools": ["web_search"],
                    }
                ]

                async with _live_session(transport, live) as session:
                    declared = _declared(live)
                    assert set(declared) == {"web_search"}
                    assert dict(declared["web_search"].parameters_json_schema) == (
                        TOOL_PARAMETERS
                    )
                    # L'Instruction de plugin est composée dans le prompt système
                    # de la Session qui déclare le Tool.
                    assert INSTRUCTION in live.instruction()

                    payload = await _call(
                        session, "web_search", {"query": "tournoi de X 2026"}
                    )

                assert payload == {
                    "status": "ok",
                    "count": 2,
                    "results": [
                        {
                            "title": "Le Monde — Tournoi de X",
                            "url": "https://example.test/tournoi",
                            "excerpt": (
                                "La finale a été remportée par l'équipe locale."
                            ),
                        },
                        {
                            "title": "France Info — Résultat",
                            "url": "https://example.test/resultat",
                            "excerpt": (
                                "Le match s'est achevé sur un score serré."
                            ),
                            "published_date": "2026-10-26",
                        },
                    ],
                }

                # La requête sortante porte les paramètres du budget de contexte
                # et les défauts du plugin quand la configuration manque.
                [request] = stub.search_requests
                assert request["method"] == "POST"
                assert request["authorization"] == f"Bearer {SECRET}"
                assert request["body"] == {
                    "query": "tournoi de X 2026",
                    "topic": "general",
                    "max_results": 10,
                    "chunks_per_source": 2,
                    "search_depth": "basic",
                    "language": "fr",
                    "country": "france",
                    "filter_by_language": False,
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                }

                # Une Trace par recherche : requête, paramètres, durée,
                # nombre de résultats, crédits. Jamais la clé.
                [search] = sink.of_kind("Search")
                assert search.payload["query"] == "tournoi de X 2026"
                assert search.payload["results"] == 2
                assert search.payload["credits"] == 1
                assert search.payload["params"]["max_results"] == 10
                assert search.payload["params"]["search_depth"] == "basic"
                assert isinstance(search.payload["duration_ms"], (int, float))
                assert SECRET not in json.dumps(sink.entries[-1].payload)
                assert all(
                    SECRET not in json.dumps(entry.payload) for entry in sink.entries
                )
                assert not sink.of_kind("SessionClosed")

    asyncio.run(scenario())


def test_configured_depth_language_and_country_reach_the_engine(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(
                tmp_path,
                sink,
                stub.base_url,
                config={
                    "search_depth": "advanced",
                    "language": "en",
                    "country": "united kingdom",
                    "strict_language": "1",
                },
            )
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    payload = await _call(
                        session, "web_search", {"query": "latest news"}
                    )

                assert payload["status"] == "ok"
                [request] = stub.search_requests
                assert request["body"]["search_depth"] == "advanced"
                assert request["body"]["language"] == "en"
                assert request["body"]["country"] == "united kingdom"
                assert request["body"]["filter_by_language"] is True
                # ``advanced`` coûte deux crédits (ADR-0018).
                assert sink.of_kind("Search")[-1].payload["credits"] == 2

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Pannes : un appel ne lève jamais, la Session et le plugin survivent.
# ---------------------------------------------------------------------------


def test_search_timeout_returns_a_french_error_and_the_session_survives(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with TavilyStub(delay=1.0) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(
                tmp_path, sink, stub.base_url, search_timeout=0.2
            )
            async with _running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with _live_session(transport, live) as session:
                    slow = await _call(
                        session, "web_search", {"query": "question lente"}
                    )
                    assert slow["status"] == "error"
                    assert "temps" in slow["message"]
                    assert sink.of_kind("Search")[-1].payload["status"] == "error"
                    assert sink.of_kind("Search")[-1].payload["credits"] == 0

                    # Le moteur répond de nouveau : la Session et le plugin
                    # n'ont pas bougé.
                    stub.delay = 0.0
                    recovered = await _call(
                        session, "web_search", {"query": "question rapide"}
                    )
                    assert recovered["status"] == "ok"
                assert app.plugins.states["websearch"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #103 — Fraîcheur et honnêteté : actualité, dates, échecs.
#
# Même seam que le reste du fichier : App → Gemini → registre → plugin → stub.
# Ce que l'on observe : le corps JSON réellement émis, la charge rendue au
# modèle et les Traces. Jamais les fonctions internes du plugin.
# ---------------------------------------------------------------------------


def test_news_topic_and_time_range_reach_the_engine(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    payload = await _call(
                        session,
                        "web_search",
                        {
                            "query": "les nouvelles de cette semaine sur X",
                            "topic": "news",
                            "time_range": "week",
                        },
                    )

                assert payload["status"] == "ok"
                # La date de publication est relayée quand le moteur la donne…
                assert payload["results"][1]["published_date"] == "2026-10-26"
                # …et absente sinon, sans faire disparaître le résultat.
                assert "published_date" not in payload["results"][0]

                [request] = stub.search_requests
                body = request["body"]
                assert body["topic"] == "news"
                assert body["time_range"] == "week"
                # Tavily refuse ``400`` un ``country`` hors ``general``.
                assert "country" not in body
                # La langue reste toujours envoyée.
                assert body["language"] == "fr"

                [search] = sink.of_kind("Search")
                assert search.payload["params"]["topic"] == "news"
                assert search.payload["params"]["time_range"] == "week"
                assert "country" not in search.payload["params"]
                assert search.payload["results"] == 2
                assert search.payload["credits"] == 1
                assert isinstance(search.payload["duration_ms"], (int, float))
                assert SECRET not in json.dumps(search.payload)

    asyncio.run(scenario())


def test_general_topic_is_the_default_and_time_range_is_omitted(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    # Le modèle ne fournit ni topic ni time_range.
                    default = await _call(
                        session, "web_search", {"query": "premier ministre irlandais"}
                    )
                    # Le modèle fournit une fenêtre mais laisse le topic au défaut.
                    windowed = await _call(
                        session,
                        "web_search",
                        {"query": "résultats du tournoi", "time_range": "month"},
                    )

                assert default["status"] == "ok"
                assert windowed["status"] == "ok"

                first, second = stub.search_requests
                assert first["body"]["topic"] == "general"
                assert "time_range" not in first["body"]
                assert first["body"]["country"] == "france"
                assert second["body"]["topic"] == "general"
                assert second["body"]["time_range"] == "month"
                assert second["body"]["country"] == "france"

                first_trace, second_trace = sink.of_kind("Search")
                assert first_trace.payload["params"]["topic"] == "general"
                assert "time_range" not in first_trace.payload["params"]
                assert second_trace.payload["params"]["time_range"] == "month"

    asyncio.run(scenario())


def test_zero_results_is_a_success_not_an_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with TavilyStub(results=()) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with _live_session(transport, live) as session:
                    payload = await _call(
                        session, "web_search", {"query": "sujet sans aucune page"}
                    )

                # Zéro résultat est un succès : à l'Instruction de dire à
                # l'oral qu'elle n'a rien trouvé.
                assert payload == {"status": "ok", "count": 0, "results": []}
                [search] = sink.of_kind("Search")
                assert search.payload["results"] == 0
                assert search.payload["credits"] == 1
                assert "status" not in search.payload
                assert app.plugins.states["websearch"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (401, "clé"),
        (429, "429"),
        (432, "432"),
        (500, "500"),
        (503, "503"),
    ],
)
def test_a_refused_search_becomes_a_french_sentence_without_a_retry(
    tmp_path: Path, status: int, fragment: str
) -> None:
    async def scenario() -> None:
        async with TavilyStub(search_status=status) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with _live_session(transport, live) as session:
                    payload = await _call(
                        session, "web_search", {"query": "question refusée"}
                    )
                    assert payload["status"] == "error"
                    assert fragment in payload["message"]
                    assert SECRET not in payload["message"]

                    # Aucune reprise automatique : un seul essai par appel.
                    assert len(stub.search_requests) == 1

                    # Le moteur se rétablit : la Session et le plugin survivent.
                    stub.search_status = 200
                    recovered = await _call(
                        session, "web_search", {"query": "question suivante"}
                    )
                    assert recovered["status"] == "ok"
                    assert len(stub.search_requests) == 2

                assert app.plugins.states["websearch"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

                failed, success = sink.of_kind("Search")
                assert failed.payload["status"] == "error"
                assert failed.payload["credits"] == 0
                assert f"HTTP {status}" in failed.payload["reason"]
                assert success.payload["results"] == 2
                assert all(
                    SECRET not in json.dumps(entry.payload) for entry in sink.entries
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw_body",
    [
        b'{"results": [',  # JSON tronqué : corps illisible.
        b'{"unexpected": true}',  # JSON valide, contrat inattendu.
        b"[]",  # JSON valide, pas un objet.
    ],
    ids=["truncated", "missing-results", "not-an-object"],
)
def test_unreadable_body_becomes_a_french_error_not_zero_results(
    tmp_path: Path, raw_body: bytes
) -> None:
    async def scenario() -> None:
        async with TavilyStub(raw_search_body=raw_body) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with _live_session(transport, live) as session:
                    payload = await _call(
                        session, "web_search", {"query": "corps illisible"}
                    )

                # Une réponse qu'on ne sait pas lire n'est pas « aucun résultat ».
                assert payload["status"] == "error"
                assert "illisible" in payload["message"]
                assert len(stub.search_requests) == 1
                assert app.plugins.states["websearch"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

                [search] = sink.of_kind("Search")
                assert search.payload["status"] == "error"
                assert search.payload["reason"] == "unreadable body"

    asyncio.run(scenario())


def test_timeout_after_eight_seconds_is_a_single_attempt(
    tmp_path: Path,
) -> None:
    """Le budget réel est de 8 s ; le test le réduit sur la copie du module."""

    async def scenario() -> None:
        async with TavilyStub(delay=1.0) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(
                tmp_path, sink, stub.base_url, search_timeout=0.2
            )
            async with _running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with _live_session(transport, live) as session:
                    payload = await _call(
                        session, "web_search", {"query": "moteur muet"}
                    )

                assert payload["status"] == "error"
                assert "temps" in payload["message"]
                # Un seul POST : pas de reprise qui doublerait la latence.
                assert len(stub.search_requests) == 1
                assert app.plugins.states["websearch"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

                [search] = sink.of_kind("Search")
                assert search.payload["status"] == "error"
                assert search.payload["reason"] == "timeout"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #104 — Budget de contexte : extraits bornés, fin propre, indicateur.
#
# Même seam que le reste du fichier : App → Gemini → registre → plugin → stub.
# La troncature se prouve par la charge rendue au modèle, jamais par une
# fonction interne.
# ---------------------------------------------------------------------------


def _long_text(length: int) -> str:
    """Texte continu de ``length`` caractères, fait de phrases terminées.

    La coupe à 3 000 caractères tombe au milieu d'une phrase : c'est au plugin
    de reculer jusqu'à la dernière fin propre.
    """
    sentence = "La finale a été remportée par l'équipe locale. "
    return (sentence * (length // len(sentence) + 1))[:length]


def test_a_long_excerpt_stops_at_600_on_a_clean_end_and_is_flagged(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        results = (
            {
                "title": "Le Monde — Tournoi de X",
                "url": "https://example.test/tournoi",
                "content": _long_text(3000),
            },
        )
        async with TavilyStub(results=results) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    payload = await _call(
                        session, "web_search", {"query": "tournoi de X"}
                    )

                assert payload["status"] == "ok"
                [result] = payload["results"]
                assert result["title"] == "Le Monde — Tournoi de X"
                assert result["url"] == "https://example.test/tournoi"
                assert len(result["excerpt"]) <= 600
                # La coupe recule jusqu'à la dernière phrase complète, sans
                # mot coupé en deux.
                assert result["excerpt"].endswith(".")
                assert result["excerpt"].endswith(
                    "La finale a été remportée par l'équipe locale."
                )
                # Le modèle sait que l'extrait est incomplet.
                assert result["truncated"] is True

    asyncio.run(scenario())


def test_excerpt_budget_never_exceeds_6000_and_keeps_every_title_and_url(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        # Un moteur qui rend plus que demandé : le budget total doit tenir
        # quand même, sans perdre un seul titre ni une seule URL.
        results = tuple(
            {
                "title": f"Source {index} — Tournoi",
                "url": f"https://example.test/source/{index}",
                "content": _long_text(3000),
            }
            for index in range(11)
        )
        async with TavilyStub(results=results) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    payload = await _call(
                        session, "web_search", {"query": "tournoi de X"}
                    )

                assert payload["status"] == "ok"
                returned = payload["results"]
                assert [(item["title"], item["url"]) for item in returned] == [
                    (item["title"], item["url"]) for item in results
                ]
                assert sum(len(item["excerpt"]) for item in returned) <= 6000
                assert all(item["truncated"] is True for item in returned)

    asyncio.run(scenario())


def test_excerpt_cut_falls_back_to_a_word_boundary_then_a_hard_cut(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        results = (
            {
                "title": "Mots",
                "url": "https://example.test/mots",
                # Aucune fin de phrase : la coupe doit reculer au dernier mot.
                "content": "alpha bravo " * 700,
            },
            {
                "title": "Bloc",
                "url": "https://example.test/bloc",
                # Aucun espace : il ne reste que la coupe nette.
                "content": "x" * 3000,
            },
            {
                "title": "Court",
                "url": "https://example.test/court",
                "content": "Texte complet.",
            },
        )
        async with TavilyStub(results=results) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    payload = await _call(
                        session, "web_search", {"query": "mots et blocs"}
                    )

                words, block, short = payload["results"]
                assert len(words["excerpt"]) <= 600
                assert words["excerpt"].endswith("bravo")
                assert words["truncated"] is True
                assert block["excerpt"] == "x" * 600
                assert block["truncated"] is True
                # Un extrait complet ne porte jamais de faux indicateur.
                assert short["excerpt"] == "Texte complet."
                assert "truncated" not in short

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #104 — Cache : une question répétée ne repaie pas un crédit.
#
# La preuve tient dans deux effets observables : le nombre de ``POST /search``
# réellement émis et le ``hit``/``miss`` de la Trace.
# ---------------------------------------------------------------------------


def test_two_identical_searches_hit_the_cache_and_save_a_credit(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    first = await _call(
                        session, "web_search", {"query": "tournoi de X 2026"}
                    )
                    second = await _call(
                        session, "web_search", {"query": "tournoi de X 2026"}
                    )

                # Même charge, sans seconde requête HTTP ni second crédit.
                assert second == first
                assert len(stub.search_requests) == 1
                miss, hit = sink.of_kind("Search")
                assert miss.payload["cache"] == "miss"
                assert miss.payload["credits"] == 1
                assert hit.payload["cache"] == "hit"
                assert hit.payload["credits"] == 0
                assert hit.payload["results"] == 2

    asyncio.run(scenario())


def test_a_reformulated_identical_question_hits_the_same_cache_entry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    await _call(
                        session, "web_search", {"query": "Tournoi de X 2026"}
                    )
                    # Casse, espaces et ponctuation finale ne changent rien.
                    reformulated = await _call(
                        session,
                        "web_search",
                        {"query": "  tournoi   de x 2026 ?! "},
                    )

                assert reformulated["status"] == "ok"
                assert len(stub.search_requests) == 1
                assert [trace.payload["cache"] for trace in sink.of_kind("Search")] == [
                    "miss",
                    "hit",
                ]

    asyncio.run(scenario())


def test_a_cached_search_expires_after_the_ttl(tmp_path: Path) -> None:
    """Le TTL réel est de 5 minutes ; le test le réduit sur la copie du module."""

    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(
                tmp_path, sink, stub.base_url, cache_ttl=0.1
            )
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    first = await _call(
                        session, "web_search", {"query": "tournoi de X 2026"}
                    )
                    await asyncio.sleep(0.2)
                    second = await _call(
                        session, "web_search", {"query": "tournoi de X 2026"}
                    )

                # Même question, mais le cache a expiré : le crédit est repayé.
                assert second == first
                assert len(stub.search_requests) == 2
                assert [trace.payload["cache"] for trace in sink.of_kind("Search")] == [
                    "miss",
                    "miss",
                ]

    asyncio.run(scenario())


def test_the_cache_stays_bounded_past_32_distinct_questions(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    for index in range(40):
                        await _call(
                            session,
                            "web_search",
                            {"query": f"question distincte {index}"},
                        )

                    # La plus ancienne est sortie : elle repaie une requête…
                    await _call(
                        session, "web_search", {"query": "question distincte 0"}
                    )
                    # …alors que la plus récente est encore là.
                    await _call(
                        session, "web_search", {"query": "question distincte 39"}
                    )

                assert len(stub.search_requests) == 41
                traces = sink.of_kind("Search")
                assert traces[-2].payload["cache"] == "miss"
                assert traces[-1].payload["cache"] == "hit"

    asyncio.run(scenario())


def test_topic_and_time_range_are_part_of_the_cache_key(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    # Même requête, trois recherches différentes pour le moteur.
                    await _call(
                        session,
                        "web_search",
                        {
                            "query": "X cette semaine",
                            "topic": "news",
                            "time_range": "week",
                        },
                    )
                    await _call(session, "web_search", {"query": "X cette semaine"})
                    await _call(
                        session,
                        "web_search",
                        {"query": "X cette semaine", "topic": "news"},
                    )
                    # La dernière est la répétition exacte de la précédente.
                    await _call(
                        session,
                        "web_search",
                        {"query": "X cette semaine", "topic": "news"},
                    )

                assert len(stub.search_requests) == 3
                assert [
                    (body["topic"], body.get("time_range"))
                    for body in (request["body"] for request in stub.search_requests)
                ] == [("news", "week"), ("general", None), ("news", None)]
                assert [
                    trace.payload["cache"] for trace in sink.of_kind("Search")
                ] == ["miss", "miss", "miss", "hit"]

    asyncio.run(scenario())


def test_language_and_depth_are_part_of_the_cache_key(tmp_path: Path) -> None:
    """Config fixée à ``create()`` : deux réglages, même cache de module.

    Aucune route de bout en bout ne fait varier la langue ou la profondeur en
    cours de vie du plugin (elles viennent du fichier d'état). Le contrat
    public ``create()`` puis le handler suffisent à prouver la clé.
    """

    async def scenario() -> None:
        async with TavilyStub() as stub:
            plugin_dir = tmp_path / "focused-plugin"
            shutil.copytree(PLUGIN_DIR, plugin_dir)
            module = _load_plugin_module(plugin_dir)
            traces: list[tuple[str, dict[str, Any]]] = []
            base = await module.create(_plugin_context(stub, traces))
            translated = await module.create(
                _plugin_context(stub, traces, language="en")
            )
            deep = await module.create(
                _plugin_context(stub, traces, search_depth="advanced")
            )
            arguments = {"query": "même question"}

            await base.tools[0].handler(arguments)
            await translated.tools[0].handler(arguments)
            await deep.tools[0].handler(arguments)
            # Mêmes réglages que le premier : celui-ci doit toucher le cache.
            await base.tools[0].handler(arguments)

            assert len(stub.search_requests) == 3
            assert [
                (body["language"], body["search_depth"])
                for body in (request["body"] for request in stub.search_requests)
            ] == [("fr", "basic"), ("en", "basic"), ("fr", "advanced")]
            searches = [
                payload for kind, payload in traces if kind == "Search"
            ]
            assert [search["cache"] for search in searches] == [
                "miss",
                "miss",
                "miss",
                "hit",
            ]

    asyncio.run(scenario())


def test_country_and_strict_language_are_part_of_the_cache_key(
    tmp_path: Path,
) -> None:
    """Même requête, même langue : seul le biais de classement change.

    ``country`` et ``strict_language`` partent dans le corps de ``POST /search``
    comme les autres réglages : deux configurations qui n'en diffèrent que par
    l'un des deux ne peuvent pas se rendre la charge de l'autre.
    """

    async def scenario() -> None:
        async with TavilyStub() as stub:
            plugin_dir = tmp_path / "focused-plugin"
            shutil.copytree(PLUGIN_DIR, plugin_dir)
            module = _load_plugin_module(plugin_dir)
            traces: list[tuple[str, dict[str, Any]]] = []
            plain = await module.create(_plugin_context(stub, traces))
            british = await module.create(
                _plugin_context(stub, traces, country="united kingdom")
            )
            strict = await module.create(
                _plugin_context(stub, traces, strict_language="1")
            )
            arguments = {"query": "même question"}

            await plain.tools[0].handler(arguments)
            await british.tools[0].handler(arguments)
            await strict.tools[0].handler(arguments)
            # Mêmes réglages que le premier : celui-ci doit toucher le cache.
            await plain.tools[0].handler(arguments)

            assert len(stub.search_requests) == 3
            assert [
                (body["country"], body["filter_by_language"])
                for body in (request["body"] for request in stub.search_requests)
            ] == [
                ("france", False),
                ("united kingdom", False),
                ("france", True),
            ]
            searches = [payload for kind, payload in traces if kind == "Search"]
            assert [search["cache"] for search in searches] == [
                "miss",
                "miss",
                "miss",
                "hit",
            ]

    asyncio.run(scenario())


def _load_plugin_module(plugin_dir: Path) -> Any:
    """Importe le point d'entrée copié, hors du chargeur, pour ``create()``."""
    module_name = "athena_plugin_websearch_focused"
    spec = importlib.util.spec_from_file_location(
        module_name, plugin_dir / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _plugin_context(
    stub: TavilyStub,
    traces: list[tuple[str, dict[str, Any]]],
    **config: str,
) -> PluginContext:
    """Contexte public minimal : une Trace en mémoire, le reste au défaut."""

    def trace(
        kind: str, payload: dict[str, Any] | None = None, *, level: str = "info"
    ) -> None:
        traces.append((kind, dict(payload or {})))

    return PluginContext(
        name="websearch",
        config={"base_url": stub.base_url, **config},
        secrets={"api_key": SECRET},
        trace=trace,
        push_event=lambda source, text: None,
    )


def test_an_error_is_never_cached(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with TavilyStub(search_status=500) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    failed = await _call(
                        session, "web_search", {"query": "question réparée"}
                    )
                    assert failed["status"] == "error"

                    # Le moteur se répare : la même question repart au moteur,
                    # sans quoi une panne passagère collerait à la question.
                    stub.search_status = 200
                    recovered = await _call(
                        session, "web_search", {"query": "question réparée"}
                    )

                assert recovered["status"] == "ok"
                assert len(stub.search_requests) == 2
                assert [
                    trace.payload["cache"] for trace in sink.of_kind("Search")
                ] == ["miss", "miss"]

    asyncio.run(scenario())


def test_unreachable_engine_during_a_session_is_a_sentence_not_a_crash(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stub = TavilyStub()
        await stub.start()
        try:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with _live_session(transport, live) as session:
                    before = await _call(
                        session, "web_search", {"query": "avant la panne"}
                    )
                    assert before["status"] == "ok"
                    await stub.stop()

                    unreachable = await _call(
                        session, "web_search", {"query": "pendant la panne"}
                    )
                    assert unreachable["status"] == "error"
                    assert "injoignable" in unreachable["message"]
                # Une panne en cours de Session n'est jamais ``PluginUnavailable``.
                assert app.plugins.states["websearch"] == "activated"
                assert not app_sink.of_kind("SessionClosed")
        finally:
            await stub.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# #105 — Recette : la porte d'Annonce reste fermée et le secret ne fuit pas.
#
# Même seam que le reste du fichier. L'Annonce se prouve à la frontière
# App/Gemini (la Connexion Live réellement ouverte) ; la non-fuite se prouve
# sur les trois surfaces observables : Traces, instantané et journal rendu.
# ---------------------------------------------------------------------------


def test_an_announcement_session_declares_no_web_search(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with TavilyStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(
                registry=registry,
                loader=loader,
                announce_prompt="ANNONCE",
                judge=FakeJudge(message="La porte est ouverte"),
            ) as (app, live, transport, app_sink):
                assert app.plugins.states["websearch"] == "activated"
                # Le Tool existe bien dans le registre : c'est le gate de
                # l'Annonce, pas une absence de plugin, qui le retire.
                assert app.tools.owner_of("web_search") == "websearch"

                async with websockets.connect(_url(transport)) as ws:
                    await register(ws, transport, client_name="desktop", drain=False)
                    await wait_until(
                        lambda: any(
                            entry.kind == "ClientRegistered"
                            for entry in app_sink.entries
                        )
                    )
                    app.push_event("matrix", "porte ouverte")
                    # La fenêtre de regroupement dure 5 s (ADR-0007) avant que
                    # le noyau n'ouvre la Connexion Live d'Annonce.
                    await wait_until(
                        lambda: bool(app_sink.of_kind("EventJudge")),
                        timeout=7.0,
                    )

                # L'instruction de plugin reste dans le prompt (ADR-0017)…
                assert INSTRUCTION in live.instruction()
                assert "ANNONCE" in live.instruction()
                # …mais la Connexion Live est ouverte sans aucun outil, donc
                # sans `web_search` : une Annonce ne peut pas chercher.
                assert live.declarations() == []
                assert app.plugins.states["websearch"] == "activated"

    asyncio.run(scenario())


def test_the_api_key_never_reaches_a_trace_a_log_or_the_snapshot(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "logs" / "server.log"
    log = FileTraceSink(log_path, min_level="debug")
    try:

        async def scenario() -> None:
            async with TavilyStub() as stub:
                # Le même journal rendu reçoit les Traces du plugin (sonde et
                # recherches) et celles de l'App (Session, ToolCall).
                plugin_sink = RecordingTraceSink()
                registry, loader = _plugins(
                    tmp_path, plugin_sink, stub.base_url, extra_sinks=[log]
                )
                async with _running_app(
                    registry=registry, loader=loader, extra_sinks=[log]
                ) as (app, live, transport, app_sink):
                    async with _live_session(transport, live) as session:
                        payload = await _call(
                            session, "web_search", {"query": "question secrète"}
                        )

                    assert payload["status"] == "ok"
                    # La clé part réellement au moteur : le test n'est pas vide.
                    assert set(stub.auth_tokens) == {f"Bearer {SECRET}"}

                    traces = [
                        entry.payload
                        for entry in (*app_sink.entries, *plugin_sink.entries)
                    ]
                    assert SECRET not in json.dumps(traces, default=str)
                    assert SECRET not in json.dumps(
                        app.snapshot(), default=str
                    )
                    assert SECRET not in json.dumps(
                        app.plugins.states, default=str
                    )

        asyncio.run(scenario())
    finally:
        log.close()

    rendered = log_path.read_text(encoding="utf-8")
    # Le journal porte bien la sonde et la recherche : l'absence de clé est
    # une vraie absence, pas un fichier vide.
    assert "CreditsRemaining" in rendered
    assert "Search" in rendered
    assert SECRET not in rendered
