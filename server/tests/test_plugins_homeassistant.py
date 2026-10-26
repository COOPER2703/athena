from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, AsyncIterator, Mapping

import websockets

from conftest import RecordingTraceSink, _url, register, wait_until
from ha_stub import (
    ASSIST_INSTRUCTION,
    DESTRUCTIVE_TOOL,
    EXPLODING_TOOL,
    LIVE_CONTEXT_TOOL,
    SCRIPT_TOOL,
    SLOW_TOOL,
    TOOL_SPECS,
    TURN_ON_TOOL,
    HaStub,
)
from protocol import SessionStart, encode
from server.adapters.gemini import GeminiLiveAdapter
from server.adapters.plugins import PluginLoader
from server.adapters.trace import emit
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.root import App
from server.app.secrets import EnvSecretResolver
from server.core.plugins import PluginContext, PluginRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "homeassistant"
TOKEN = "ha-long-lived-test-token"
TOKEN_VARIABLE = "ATHENA_PLUGIN_HOMEASSISTANT_TOKEN"


# ---------------------------------------------------------------------------
# Faux Gemini et transport : mêmes formes que ``test_app_integration``.
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


@contextlib.asynccontextmanager
async def _running_app(
    *,
    registry: PluginRegistry | None = None,
    loader: PluginLoader | None = None,
) -> AsyncIterator[tuple[Any, _ToolLiveConnect, WebSocketTransport, RecordingTraceSink]]:
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


def _install_homeassistant(tmp_path: Path, url: str) -> tuple[Path, Path]:
    """Copie le vrai plugin et écrit un fichier d'état qui l'active."""
    plugins_dir = tmp_path / "plugins"
    shutil.copytree(PLUGIN_DIR, plugins_dir / "homeassistant")
    state_path = tmp_path / "plugins.json"
    state_path.write_text(
        json.dumps(
            {
                "enabled": ["homeassistant"],
                "config": {"homeassistant": {"url": url}},
            }
        ),
        encoding="utf-8",
    )
    return plugins_dir, state_path


def _plugin_module() -> ModuleType:
    """Importe le vrai module du plugin, hors chargeur, pour patcher ses budgets."""
    name = "homeassistant_plugin_under_test"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _context(
    url: str,
    sink: RecordingTraceSink,
    *,
    token: str = TOKEN,
) -> PluginContext:
    def trace(
        kind: str,
        payload: Mapping[str, object] | None = None,
        *,
        level: str = "info",
    ) -> None:
        emit(
            sink,
            "plugins",
            kind,
            payload={"plugin": "homeassistant", **dict(payload or {})},
            level=level,
        )

    return PluginContext(
        name="homeassistant",
        config={"url": url},
        secrets={"token": token},
        trace=trace,
        push_event=lambda source, content: None,
    )


async def _call(session: _ScriptedSession, name: str, arguments: dict) -> dict:
    before = len(session.function_responses)
    session.call_tool(f"call-{before}", name, arguments)
    await wait_until(lambda: len(session.function_responses) > before)
    return dict(session.function_responses[-1].response)


def _declared(live: _ToolLiveConnect) -> dict[str, Any]:
    return {decl.name: decl for decl in live.declarations()}


# ---------------------------------------------------------------------------
# Manifeste
# ---------------------------------------------------------------------------


def test_manifest_declares_only_static_identity_and_requirements() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    assert manifest == {
        "api_version": 1,
        "name": "homeassistant",
        "version": "0.1.0",
        "description": "Pilote Home Assistant via son serveur MCP",
        "platform": "server",
        "entrypoint": "plugin.py",
        "config": [{"key": "url", "type": "url", "required": True}],
        "secrets": [{"key": "token", "required": True}],
    }


# ---------------------------------------------------------------------------
# Seam A — application réelle, plugin réel, serveur MCP réel en processus.
# ---------------------------------------------------------------------------


def test_homeassistant_tools_are_declared_and_called_like_home_assistant(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with HaStub() as stub:
            sink = RecordingTraceSink()
            plugins_dir, state_path = _install_homeassistant(
                tmp_path, stub.base_url + "/"
            )
            registry = PluginRegistry()
            loader = PluginLoader(
                directory=plugins_dir,
                state_path=state_path,
                secrets=EnvSecretResolver({TOKEN_VARIABLE: TOKEN}),
                trace_sink=sink,
            )
            async with _running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                [state] = sink.of_kind("PluginState")
                assert state.payload["state"] == "activated"
                assert state.payload["tools"] == len(TOOL_SPECS)
                assert app.plugins.instruction() == ASSIST_INSTRUCTION

                async with _live_session(transport, live) as session:
                    declared = _declared(live)
                    assert set(declared) == {spec.name for spec in TOOL_SPECS}
                    for spec in TOOL_SPECS:
                        assert declared[spec.name].description == spec.description
                        assert dict(
                            declared[spec.name].parameters_json_schema
                        ) == spec.parameters

                    assert await _call(
                        session,
                        TURN_ON_TOOL,
                        {"name": "Salon", "domain": "light"},
                    ) == {"status": "ok", "message": "Turned on Salon"}
                    assert stub.calls[-1] == {
                        "tool": TURN_ON_TOOL,
                        "arguments": {"name": "Salon", "domain": "light"},
                    }

                    # Lecture d'état et script : exposés et appelables comme un Intent.
                    assert await _call(session, LIVE_CONTEXT_TOOL, {}) == {
                        "status": "ok",
                        "message": "The garage door is closed.",
                    }
                    assert await _call(session, SCRIPT_TOOL, {}) == {
                        "status": "ok",
                        "message": "Started script movie_night",
                    }

                    # Une annotation destructive ne bloque pas l'appel (v1).
                    assert await _call(session, DESTRUCTIVE_TOOL, {}) == {
                        "status": "ok",
                        "message": "Deleted every exposed entity",
                    }

                    assert not sink.of_kind("SessionClosed")

                # Le token part sur chaque requête et ne fuit jamais dans la trace.
                assert stub.auth_tokens
                assert set(stub.auth_tokens) == {f"Bearer {TOKEN}"}
                assert all(
                    TOKEN not in json.dumps(entry.payload) for entry in sink.entries
                )

                # Les annotations sont tracées telles que HA les publie.
                annotations = {
                    entry.payload["tool"]: entry.payload
                    for entry in sink.of_kind("ToolAnnotations")
                }
                assert set(annotations) == {spec.name for spec in TOOL_SPECS}
                assert annotations[LIVE_CONTEXT_TOOL]["read_only"] is True
                assert annotations[DESTRUCTIVE_TOOL]["destructive"] is True

    asyncio.run(scenario())


def test_home_assistant_error_text_reaches_the_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with HaStub() as stub:
            sink = RecordingTraceSink()
            plugins_dir, state_path = _install_homeassistant(tmp_path, stub.base_url)
            registry = PluginRegistry()
            loader = PluginLoader(
                directory=plugins_dir,
                state_path=state_path,
                secrets=EnvSecretResolver({TOKEN_VARIABLE: TOKEN}),
                trace_sink=sink,
            )
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    # Le texte de HA remonte, sans l'enveloppe du SDK.
                    assert await _call(session, EXPLODING_TOOL, {}) == {
                        "status": "error",
                        "message": "Home Assistant refused: entity not found",
                    }
                    # La Session survit à l'isError : l'appel suivant passe.
                    assert await _call(
                        session, TURN_ON_TOOL, {"name": "Bureau"}
                    ) == {"status": "ok", "message": "Turned on Bureau"}
                    assert not sink.of_kind("SessionClosed")

    asyncio.run(scenario())


def test_absent_instruction_is_traced_but_tools_are_still_served() -> None:
    async def scenario() -> None:
        module = _plugin_module()
        sink = RecordingTraceSink()
        async with HaStub(with_prompt=False) as stub:
            binding = await module.create(_context(stub.base_url, sink))
            assert binding.instruction == ""
            assert {tool.spec.name for tool in binding.tools} == {
                spec.name for spec in TOOL_SPECS
            }
            assert sink.of_kind("InstructionUnavailable")

            registry = PluginRegistry()
            registry.add(binding)
            async with _running_app(registry=registry) as (_, live, transport, _):
                async with _live_session(transport, live) as session:
                    assert await _call(session, TURN_ON_TOOL, {"name": "Salon"}) == {
                        "status": "ok",
                        "message": "Turned on Salon",
                    }

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Délais : les budgets sont réduits dans le test, les valeurs de prod intactes.
# ---------------------------------------------------------------------------


def test_discovery_timeout_starts_without_tools_and_keeps_the_session_alive(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        module = _plugin_module()
        monkeypatch.setattr(module, "DISCOVERY_TIMEOUT", 0.3)
        sink = RecordingTraceSink()
        async with HaStub(delay=1.0) as stub:
            binding = await module.create(_context(stub.base_url, sink))
            assert binding.tools == ()
            assert binding.instruction == ""
            assert sink.of_kind("DiscoveryFailed")
            assert sink.of_kind("PluginReady")[0].payload["tools"] == 0

            registry = PluginRegistry()
            registry.add(binding)
            async with _running_app(registry=registry) as (_, live, transport, _):
                async with _live_session(transport, live) as session:
                    result = await _call(session, TURN_ON_TOOL, {"name": "Salon"})
                    assert result["status"] == "error"
                    assert TURN_ON_TOOL in result["message"]
                    assert not sink.of_kind("SessionClosed")

    asyncio.run(scenario())


def test_call_timeout_returns_an_error_and_the_session_survives(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        module = _plugin_module()
        monkeypatch.setattr(module, "CALL_TIMEOUT", 0.3)
        sink = RecordingTraceSink()
        async with HaStub() as stub:
            registry = PluginRegistry()
            registry.add(await module.create(_context(stub.base_url, sink)))
            async with _running_app(registry=registry) as (_, live, transport, _):
                async with _live_session(transport, live) as session:
                    slow = await _call(session, SLOW_TOOL, {})
                    assert slow["status"] == "error"
                    assert "temps" in slow["message"]

                    # La session MCP a été refermée, pas la Session d'Athena.
                    assert await _call(
                        session, TURN_ON_TOOL, {"name": "Salon"}
                    ) == {"status": "ok", "message": "Turned on Salon"}
                    assert not sink.of_kind("SessionClosed")

    asyncio.run(scenario())


def test_unreachable_home_assistant_keeps_the_session_alive() -> None:
    async def scenario() -> None:
        module = _plugin_module()
        sink = RecordingTraceSink()
        stub = HaStub()
        await stub.start()
        try:
            registry = PluginRegistry()
            registry.add(await module.create(_context(stub.base_url, sink)))
            async with _running_app(registry=registry) as (_, live, transport, _):
                async with _live_session(transport, live) as session:
                    assert await _call(session, TURN_ON_TOOL, {"name": "Salon"}) == {
                        "status": "ok",
                        "message": "Turned on Salon",
                    }
                    await stub.stop()

                    unreachable = await _call(session, TURN_ON_TOOL, {"name": "Salon"})
                    assert unreachable["status"] == "error"
                    assert "injoignable" in unreachable["message"]
                    assert not sink.of_kind("SessionClosed")
        finally:
            await stub.stop()

    asyncio.run(scenario())


def test_home_assistant_restart_between_two_calls_needs_no_athena_restart() -> None:
    async def scenario() -> None:
        module = _plugin_module()
        sink = RecordingTraceSink()
        first = HaStub()
        await first.start()
        second: HaStub | None = None
        try:
            registry = PluginRegistry()
            registry.add(await module.create(_context(first.base_url, sink)))
            async with _running_app(registry=registry) as (_, live, transport, _):
                async with _live_session(transport, live) as session:
                    assert await _call(
                        session, TURN_ON_TOOL, {"name": "Salon"}
                    ) == {"status": "ok", "message": "Turned on Salon"}

                    # HA redémarre sur le même port ; Athena ne bouge pas.
                    port = first.port
                    await first.stop()
                    second = HaStub(port=port)
                    await second.start()

                    assert await _call(
                        session, TURN_ON_TOOL, {"name": "Cuisine"}
                    ) == {"status": "ok", "message": "Turned on Cuisine"}
                    assert second.calls[-1] == {
                        "tool": TURN_ON_TOOL,
                        "arguments": {"name": "Cuisine", "domain": ""},
                    }
                    assert not sink.of_kind("SessionClosed")
        finally:
            await first.stop()
            if second is not None:
                await second.stop()

    asyncio.run(scenario())
