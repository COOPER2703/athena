"""Harnais partagé des tests du plugin ``notion``.

Mêmes formes que ``test_plugins_websearch.py`` : un faux Gemini Live retient
les Configs ouvertes, l'App tourne pour de vrai sur une socket éphémère, et le
plugin réel est copié dans ``tmp_path`` pour être activé par ``plugins.json``.
Les projections d'objets Notion et la session de test vivent ici, une seule
fois, pour tous les tests du plugin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Sequence

import websockets

from conftest import RecordingTraceSink, _url, register, wait_until
from protocol import SessionStart, encode
from server.adapters.gemini import GeminiLiveAdapter
from server.adapters.plugins import PluginLoader
from server.adapters.transport import WebSocketTransport
from server.app.clock import AsyncioClock
from server.app.root import App
from server.app.secrets import EnvSecretResolver
from server.app.trace import FanOutTraceSink
from server.core.plugins import PluginRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "notion"

SECRET = "secret-notion-long-lived-test-token"
SECRET_VARIABLE = "ATHENA_PLUGIN_NOTION_TOKEN"

# Identifiant de page par défaut des tests : 32 caractères hexadécimaux.
DEFAULT_PARENT = "0123456789abcdef0123456789abcdef"


class ScriptedSession:
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
    def __init__(self, session: ScriptedSession) -> None:
        self._session = session

    async def __aenter__(self) -> ScriptedSession:
        return self._session

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class ToolLiveConnect:
    """Fake ``live_connect`` : retient les Configs et les sessions ouvertes."""

    def __init__(self) -> None:
        self.configs: list[Any] = []
        self.sessions: list[ScriptedSession] = []

    def __call__(self, model: str, config: Any) -> _ScriptedContext:
        session = ScriptedSession()
        self.configs.append(config)
        self.sessions.append(session)
        return _ScriptedContext(session)

    @property
    def session(self) -> ScriptedSession:
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
async def running_app(
    *,
    registry: PluginRegistry | None = None,
    loader: PluginLoader | None = None,
    retry_interval: float = 5.0,
    announce_prompt: str = "",
    judge: Any = None,
    extra_sinks: Sequence[Any] = (),
) -> AsyncIterator[
    tuple[Any, ToolLiveConnect, WebSocketTransport, RecordingTraceSink]
]:
    """Démarre l'App réelle avec le faux Gemini et rend la couture observable."""
    sink = RecordingTraceSink()
    trace_sink = FanOutTraceSink(sink, *extra_sinks) if extra_sinks else sink
    transport = WebSocketTransport(host="127.0.0.1", port=0, trace_sink=trace_sink)
    live = ToolLiveConnect()
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
async def live_session(
    transport: WebSocketTransport, live: ToolLiveConnect
) -> AsyncIterator[ScriptedSession]:
    """Ouvre une vraie Connexion Live, enregistre le client, démarre la Session."""
    async with websockets.connect(_url(transport)) as ws:
        await register(ws, transport, drain=False)
        await ws.send(encode(SessionStart()))
        await wait_until(lambda: bool(live.sessions))
        yield live.session


def patched_constant(source: str, name: str, value: float) -> str:
    """Réduit un budget du module copié ; le fichier du dépôt n'est pas touché."""
    pattern = re.compile(rf"^{name} = [0-9.]+$", re.MULTILINE)
    patched, count = pattern.subn(f"{name} = {value!r}", source, count=1)
    assert count == 1, f"{name} not found in the plugin source"
    return patched


def install_notion(
    tmp_path: Path,
    base_url: str,
    *,
    parent: str = DEFAULT_PARENT,
    config: dict[str, str] | None = None,
    timeout: float | None = None,
) -> tuple[Path, Path]:
    """Copie le vrai plugin et écrit un fichier d'état qui l'active."""
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "notion"
    shutil.copytree(PLUGIN_DIR, plugin_dir)
    if timeout is not None:
        source = (plugin_dir / "plugin.py").read_text(encoding="utf-8")
        source = patched_constant(source, "REQUEST_TIMEOUT", timeout)
        (plugin_dir / "plugin.py").write_text(source, encoding="utf-8")

    state_path = tmp_path / "plugins.json"
    state_path.write_text(
        json.dumps(
            {
                "enabled": ["notion"],
                "config": {
                    "notion": {
                        "base_url": base_url,
                        "parent_page_id": parent,
                        **(config or {}),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return plugins_dir, state_path


def build_plugins(
    tmp_path: Path,
    sink: RecordingTraceSink,
    base_url: str,
    *,
    parent: str = DEFAULT_PARENT,
    config: dict[str, str] | None = None,
    secret: str | None = SECRET,
    timeout: float | None = None,
    extra_sinks: Sequence[Any] = (),
) -> tuple[PluginRegistry, PluginLoader]:
    """Registre + chargeur réels branchés sur le plugin copié et le faux serveur."""
    plugins_dir, state_path = install_notion(
        tmp_path,
        base_url,
        parent=parent,
        config=config,
        timeout=timeout,
    )
    registry = PluginRegistry()
    trace_sink = FanOutTraceSink(sink, *extra_sinks) if extra_sinks else sink
    secrets = {} if secret is None else {SECRET_VARIABLE: secret}
    loader = PluginLoader(
        directory=plugins_dir,
        state_path=state_path,
        secrets=EnvSecretResolver(secrets),
        trace_sink=trace_sink,
    )
    return registry, loader


async def call_tool(
    session: ScriptedSession, name: str, arguments: dict
) -> dict:
    """Pousse un appel de Tool et attend la réponse rendue au modèle."""
    before = len(session.function_responses)
    session.call_tool(f"call-{before}", name, arguments)
    await wait_until(lambda: len(session.function_responses) > before)
    return dict(session.function_responses[-1].response)


def declared(live: ToolLiveConnect) -> dict[str, Any]:
    """Les déclarations de la dernière Connexion Live, indexées par nom."""
    return {decl.name: decl for decl in live.declarations()}


def states(sink: RecordingTraceSink) -> list[str]:
    """Les états de plugin tracés, dans l'ordre."""
    return [str(entry.payload["state"]) for entry in sink.of_kind("PluginState")]


def program_probe(
    stub: Any,
    parent: str = DEFAULT_PARENT,
    *,
    users_status: int = 200,
    parent_status: int = 200,
) -> None:
    """Programme les deux routes de la sonde de démarrage."""
    stub.route(
        "GET",
        "/v1/users/me",
        status=users_status,
        body={"object": "user", "id": "bot"},
    )
    stub.route(
        "GET",
        f"/v1/pages/{parent}",
        status=parent_status,
        body={"object": "page", "id": parent},
    )


def page_object(
    page_id: str,
    title: str = "Journal de bord",
    *,
    key: str = "Titre",
    url: str | None = None,
    edited: str = "2026-10-01T10:00:00.000Z",
    properties: dict[str, Any] | None = None,
) -> dict:
    """Objet page Notion minimal, avec sa propriété de titre nommable."""
    rendered: dict[str, Any] = {
        key: {
            "id": "title",
            "type": "title",
            "title": [
                {
                    "type": "text",
                    "plain_text": title,
                    "text": {"content": title},
                }
            ],
        }
    }
    rendered.update(properties or {})
    return {
        "object": "page",
        "id": page_id,
        "url": url if url is not None else f"https://www.notion.so/{page_id}",
        "last_edited_time": edited,
        "properties": rendered,
    }


def markdown_body(
    markdown: str,
    *,
    truncated: bool = False,
    unknown_block_ids: list[str] | None = None,
) -> dict:
    """Réponse de ``GET /v1/pages/{id}/markdown``."""
    return {
        "object": "page_markdown",
        "id": "page",
        "markdown": markdown,
        "truncated": truncated,
        "unknown_block_ids": unknown_block_ids or [],
    }


@contextlib.asynccontextmanager
async def notion_session(
    tmp_path: Path,
    stub: Any,
    *,
    sink: RecordingTraceSink | None = None,
    timeout: float | None = None,
) -> AsyncIterator[
    tuple[Any, Any, Any, RecordingTraceSink, RecordingTraceSink]
]:
    """App réelle, Connexion Live, plugin réel branché sur le faux serveur."""
    sink = sink if sink is not None else RecordingTraceSink()
    registry, loader = build_plugins(
        tmp_path, sink, stub.base_url, timeout=timeout
    )
    async with running_app(registry=registry, loader=loader) as (
        app,
        live,
        transport,
        app_sink,
    ):
        async with live_session(transport, live) as session:
            yield app, live, session, sink, app_sink
