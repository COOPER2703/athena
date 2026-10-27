from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
import websockets

from conftest import RecordingTraceSink, _url, register, wait_until
from protocol import SessionStart, encode
from server.adapters.gemini import GeminiLiveAdapter
from server.adapters.plugins import PluginLoader
from server.adapters.plugins.loader import PluginLoadError
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.root import App
from server.app.secrets import EnvSecretResolver
from server.core.plugins import PluginRegistry
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
) -> AsyncIterator[
    tuple[Any, _ToolLiveConnect, WebSocketTransport, RecordingTraceSink]
]:
    sink = RecordingTraceSink()
    transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=sink)
    live = _ToolLiveConnect()
    gemini = GeminiLiveAdapter(
        api_key="test-key",
        model="gemini-live",
        voice="Aoede",
        live_connect=live,
        trace_sink=sink,
    )
    app = App(
        transport=transport,
        gemini=gemini,
        clock=AsyncioClock(),
        trace_sink=sink,
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
) -> tuple[Path, Path]:
    """Copie le vrai plugin et écrit un fichier d'état qui l'active."""
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "websearch"
    shutil.copytree(PLUGIN_DIR, plugin_dir)
    if search_timeout is not None:
        source = (plugin_dir / "plugin.py").read_text(encoding="utf-8")
        source = _patched_constant(source, "SEARCH_TIMEOUT", search_timeout)
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
) -> tuple[PluginRegistry, PluginLoader]:
    plugins_dir, state_path = _install_websearch(
        tmp_path,
        base_url,
        config=config,
        search_timeout=search_timeout,
    )
    registry = PluginRegistry()
    loader = PluginLoader(
        directory=plugins_dir,
        state_path=state_path,
        secrets=EnvSecretResolver({SECRET_VARIABLE: secret}),
        trace_sink=sink,
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

