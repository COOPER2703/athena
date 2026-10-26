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
from typing import Any, AsyncIterator

import pytest
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
from plugin_api import PluginContext, PluginUnavailable
from protocol import SessionStart, encode
from server.adapters.gemini import GeminiLiveAdapter
from server.adapters.plugins import PluginLoader
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.root import App
from server.app.secrets import EnvSecretResolver
from server.core.plugins import PluginRegistry

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


def _patched_constant(source: str, name: str, value: float) -> str:
    """Réduit un budget du module copié ; le fichier du dépôt n'est pas touché."""
    pattern = re.compile(rf"^{name} = [0-9.]+$", re.MULTILINE)
    patched, count = pattern.subn(f"{name} = {value!r}", source, count=1)
    assert count == 1, f"{name} not found in the plugin source"
    return patched


def _install_homeassistant(
    tmp_path: Path,
    url: str,
    *,
    discovery_timeout: float | None = None,
    call_timeout: float | None = None,
) -> tuple[Path, Path]:
    """Copie le vrai plugin et écrit un fichier d'état qui l'active."""
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "homeassistant"
    shutil.copytree(PLUGIN_DIR, plugin_dir)
    if discovery_timeout is not None or call_timeout is not None:
        source = (plugin_dir / "plugin.py").read_text(encoding="utf-8")
        if discovery_timeout is not None:
            source = _patched_constant(source, "DISCOVERY_TIMEOUT", discovery_timeout)
        if call_timeout is not None:
            source = _patched_constant(source, "CALL_TIMEOUT", call_timeout)
        (plugin_dir / "plugin.py").write_text(source, encoding="utf-8")

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


def _plugins(
    tmp_path: Path,
    sink: RecordingTraceSink,
    url: str,
    *,
    discovery_timeout: float | None = None,
    call_timeout: float | None = None,
) -> tuple[PluginRegistry, PluginLoader]:
    plugins_dir, state_path = _install_homeassistant(
        tmp_path,
        url,
        discovery_timeout=discovery_timeout,
        call_timeout=call_timeout,
    )
    registry = PluginRegistry()
    loader = PluginLoader(
        directory=plugins_dir,
        state_path=state_path,
        secrets=EnvSecretResolver({TOKEN_VARIABLE: TOKEN}),
        trace_sink=sink,
    )
    return registry, loader


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


def test_unreachable_discovery_raises_the_retryable_error() -> None:
    """Une maison injoignable lève ``PluginUnavailable``, jamais un défaut fatal."""
    spec = importlib.util.spec_from_file_location(
        "homeassistant_plugin_under_test", PLUGIN_DIR / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    context = PluginContext(
        name="homeassistant",
        config={"url": "http://127.0.0.1:1"},
        secrets={"token": TOKEN},
        trace=lambda *args, **kwargs: None,
        push_event=lambda source, content: None,
    )

    with pytest.raises(PluginUnavailable) as excinfo:
        asyncio.run(module.create(context))

    assert "discovery failed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Seam A — application réelle, plugin réel, serveur MCP réel en processus.
# ---------------------------------------------------------------------------


def test_homeassistant_tools_are_declared_and_called_like_home_assistant(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with HaStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url + "/")
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
                    # Le prompt Assist, lu via ``prompts/get``, est composé dans
                    # le prompt système de la Session qui déclare ses Tools.
                    assert ASSIST_INSTRUCTION in live.instruction()
                    for spec in TOOL_SPECS:
                        assert declared[spec.name].description == spec.description
                        assert (
                            dict(declared[spec.name].parameters_json_schema)
                            == spec.parameters
                        )

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
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
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


def test_absent_instruction_is_traced_but_tools_are_still_served(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with HaStub(with_prompt=False) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                assert app.plugins.instruction() == ""
                assert sink.of_kind("InstructionUnavailable")

                async with _live_session(transport, live) as session:
                    assert set(_declared(live)) == {spec.name for spec in TOOL_SPECS}
                    # Sans instruction, le prompt reste vide mais les Tools
                    # sont bien servis : l'absence n'est jamais bloquante.
                    assert live.instruction() == ""
                    assert await _call(session, TURN_ON_TOOL, {"name": "Salon"}) == {
                        "status": "ok",
                        "message": "Turned on Salon",
                    }

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Délais : copies temporaires du module avec des budgets réduits.
# ---------------------------------------------------------------------------


def test_discovery_timeout_degrades_the_start_then_repairs_itself(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with HaStub(delay=1.0) as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(
                tmp_path, sink, stub.base_url, discovery_timeout=0.3
            )
            async with _running_app(
                registry=registry, loader=loader, retry_interval=0.1
            ) as (app, live, transport, _):
                # HA dort : le plugin échoue au démarrage, Athena démarre quand même.
                assert _states(sink) == ["failed"]
                assert registry.tools.specs() == ()
                assert sink.of_kind("DiscoveryFailed")

                async with _live_session(transport, live) as session:
                    missing = await _call(session, TURN_ON_TOOL, {"name": "Salon"})
                    assert missing["status"] == "error"
                    assert TURN_ON_TOOL in missing["message"]

                    # HA répond de nouveau : la retente de fond répare la maison.
                    stub.delay = 0.0
                    await wait_until(
                        lambda: any(
                            entry.payload["state"] == "activated"
                            for entry in sink.of_kind("PluginState")
                        )
                    )
                    assert app.plugins.instruction() == ASSIST_INSTRUCTION
                    assert await _call(session, TURN_ON_TOOL, {"name": "Salon"}) == {
                        "status": "ok",
                        "message": "Turned on Salon",
                    }
                    assert not sink.of_kind("SessionClosed")

    asyncio.run(scenario())


def test_call_timeout_returns_an_error_and_the_session_survives(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with HaStub() as stub:
            sink = RecordingTraceSink()
            registry, loader = _plugins(
                tmp_path, sink, stub.base_url, call_timeout=0.3
            )
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
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


def test_unreachable_home_assistant_keeps_the_session_alive(tmp_path: Path) -> None:
    async def scenario() -> None:
        module_sink = RecordingTraceSink()
        stub = HaStub()
        await stub.start()
        try:
            registry, loader = _plugins(tmp_path, module_sink, stub.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with _live_session(transport, live) as session:
                    assert await _call(session, TURN_ON_TOOL, {"name": "Salon"}) == {
                        "status": "ok",
                        "message": "Turned on Salon",
                    }
                    await stub.stop()

                    unreachable = await _call(session, TURN_ON_TOOL, {"name": "Salon"})
                    assert unreachable["status"] == "error"
                    assert "injoignable" in unreachable["message"]
                    assert not module_sink.of_kind("SessionClosed")
        finally:
            await stub.stop()

    asyncio.run(scenario())


def test_home_assistant_restart_between_two_calls_needs_no_athena_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first = HaStub()
        await first.start()
        second: HaStub | None = None
        try:
            sink = RecordingTraceSink()
            registry, loader = _plugins(tmp_path, sink, first.base_url)
            async with _running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
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
