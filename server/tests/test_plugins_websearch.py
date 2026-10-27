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
    secret: str = SECRET,
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
        secret=secret,
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
                assert _states(sink) == ["failed"]
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

