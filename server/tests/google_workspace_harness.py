"""Harnais partagé des tests du plugin ``google-workspace``.

Mêmes formes que ``notion_harness.py`` : un faux Gemini Live retient les
Configs ouvertes, l'App tourne pour de vrai sur une socket éphémère, et le
plugin réel est copié dans ``tmp_path`` pour être activé par ``plugins.json``.
Les secrets multi-comptes, les fabriques d'objets Gmail et la session de test
vivent ici, une seule fois, pour tous les tests du plugin.

Points d'extension des tickets suivants : ``install_google_workspace`` accepte
une configuration et des secrets arbitraires, ``patched_constant`` réduit un
budget du module copié, et les fabriques ``gmail_*`` / ``calendar_*`` rendent
les objets que les nouveaux Tools liront.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, Sequence
from types import SimpleNamespace

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
PLUGIN_DIR = REPO_ROOT / "plugins" / "google-workspace"

# -- Secrets de test, trois listes parallèles à ``accounts`` -----------------

CLIENT_ID_VARIABLE = "ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_ID"
CLIENT_SECRET_VARIABLE = "ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_SECRET"
REFRESH_TOKEN_VARIABLE = "ATHENA_PLUGIN_GOOGLE_WORKSPACE_REFRESH_TOKEN"

CLIENT_ID_PRO = "client-id-pro"
CLIENT_ID_PERSO = "client-id-perso"
CLIENT_SECRET_PRO = "client-secret-pro"
CLIENT_SECRET_PERSO = "client-secret-perso"
REFRESH_TOKEN_PRO = "refresh-token-pro"
REFRESH_TOKEN_PERSO = "refresh-token-perso"

CLIENT_IDS = f"{CLIENT_ID_PRO},{CLIENT_ID_PERSO}"
CLIENT_SECRETS = f"{CLIENT_SECRET_PRO},{CLIENT_SECRET_PERSO}"
REFRESH_TOKENS = f"{REFRESH_TOKEN_PRO},{REFRESH_TOKEN_PERSO}"

ACCOUNTS = "Pro,perso"
DEFAULT_ACCOUNT = "perso"

# Jetons d'accès distincts pour observer quel compte a servi.
ACCESS_TOKEN_PRO = "access-token-pro"
ACCESS_TOKEN_PERSO = "access-token-perso"

DEFAULT_SECRETS: dict[str, str] = {
    CLIENT_ID_VARIABLE: CLIENT_IDS,
    CLIENT_SECRET_VARIABLE: CLIENT_SECRETS,
    REFRESH_TOKEN_VARIABLE: REFRESH_TOKENS,
}


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


def install_google_workspace(
    tmp_path: Path,
    base_url: str,
    *,
    token_url: str | None = None,
    accounts: str = ACCOUNTS,
    default_account: str | None = DEFAULT_ACCOUNT,
    config: Mapping[str, str] | None = None,
    constants: Mapping[str, float] | None = None,
) -> tuple[Path, Path]:
    """Copie le vrai plugin et écrit un fichier d'état qui l'active."""
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "google-workspace"
    shutil.copytree(PLUGIN_DIR, plugin_dir)
    if constants:
        source = (plugin_dir / "plugin.py").read_text(encoding="utf-8")
        for name, value in constants.items():
            source = patched_constant(source, name, value)
        (plugin_dir / "plugin.py").write_text(source, encoding="utf-8")

    plugin_config: dict[str, str] = {
        "accounts": accounts,
        "api_base_url": base_url,
        "token_url": token_url if token_url is not None else base_url + "/token",
    }
    if default_account is not None:
        plugin_config["default_account"] = default_account
    plugin_config.update(config or {})

    state_path = tmp_path / "plugins.json"
    state_path.write_text(
        json.dumps(
            {
                "enabled": ["google-workspace"],
                "config": {"google-workspace": plugin_config},
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
    token_url: str | None = None,
    accounts: str = ACCOUNTS,
    default_account: str | None = DEFAULT_ACCOUNT,
    config: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    constants: Mapping[str, float] | None = None,
    extra_sinks: Sequence[Any] = (),
) -> tuple[PluginRegistry, PluginLoader]:
    """Registre + chargeur réels branchés sur le plugin copié et le faux serveur."""
    plugins_dir, state_path = install_google_workspace(
        tmp_path,
        base_url,
        token_url=token_url,
        accounts=accounts,
        default_account=default_account,
        config=config,
        constants=constants,
    )
    registry = PluginRegistry()
    trace_sink = FanOutTraceSink(sink, *extra_sinks) if extra_sinks else sink
    resolved = dict(DEFAULT_SECRETS)
    if secrets is not None:
        resolved.update(secrets)
    loader = PluginLoader(
        directory=plugins_dir,
        state_path=state_path,
        secrets=EnvSecretResolver(resolved),
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


async def wait_for_state(
    app: Any, plugin: str, state: str, timeout: float = 2.0
) -> None:
    await wait_until(
        lambda: app.plugins.states.get(plugin) == state, timeout=timeout
    )


# -- Réponses Google préprogrammées -----------------------------------------


def program_tokens(
    stub: Any, access_tokens: Mapping[str, str] | None = None
) -> None:
    """Programme un échange réussi par refresh token, jetons distincts."""
    tokens = dict(
        access_tokens
        or {
            REFRESH_TOKEN_PRO: ACCESS_TOKEN_PRO,
            REFRESH_TOKEN_PERSO: ACCESS_TOKEN_PERSO,
        }
    )
    for refresh_token, access_token in tokens.items():
        stub.route_token(refresh_token, access_token=access_token)


def gmail_metadata(
    message_id: str,
    *,
    thread_id: str | None = None,
    sender: str = "Camille <camille@example.test>",
    subject: str = "Déjeuner jeudi",
    date: str = "Thu, 5 Nov 2026 10:00:00 +0100",
    snippet: str = "On se retrouve à midi ?",
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Objet Gmail minimal rendu par ``GET /messages/{id}`` en métadonnées."""
    rendered = {
        "From": sender,
        "Subject": subject,
        "Date": date,
        **(headers or {}),
    }
    return {
        "id": message_id,
        "threadId": thread_id if thread_id is not None else f"thread-{message_id}",
        "snippet": snippet,
        "payload": {
            "headers": [
                {"name": name, "value": value}
                for name, value in rendered.items()
            ]
        },
    }


def gmail_message_list(
    *messages: Mapping[str, Any], next_page_token: str | None = None
) -> dict[str, Any]:
    """Objet Gmail rendu par ``GET /messages`` (liste de métadonnées)."""
    body: dict[str, Any] = {
        "messages": [
            {
                "id": message.get("id"),
                "threadId": message.get("threadId"),
            }
            for message in messages
        ]
    }
    if next_page_token is not None:
        body["nextPageToken"] = next_page_token
    return body


def _gmail_encoded(text: str) -> str:
    """Corps Gmail : base64url sans remplissage, comme l'API le rend."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def _gmail_body_part(mime_type: str, text: str) -> dict[str, Any]:
    """Partie Gmail portant un corps texte encodé."""
    return {
        "mimeType": mime_type,
        "body": {
            "size": len(text.encode("utf-8")),
            "data": _gmail_encoded(text),
        },
    }


def gmail_read_message(
    message_id: str,
    *,
    thread_id: str | None = None,
    sender: str = "Camille <camille@example.test>",
    recipients: str = "Cooper <cooper@example.test>",
    cc: str | None = None,
    subject: str = "Déjeuner jeudi",
    date: str = "Thu, 5 Nov 2026 10:00:00 +0100",
    plain: str | None = "On se retrouve à midi ?",
    html: str | None = None,
    attachments: Sequence[str] = (),
) -> dict[str, Any]:
    """Objet Gmail complet rendu par ``GET /messages/{id}?format=full``.

    ``plain`` et ``html`` peuvent coexister (``multipart/alternative``) ; ``html``
    seul exerce le repli nettoyé. Les pièces jointes n'ont qu'un nom et un
    identifiant : aucun contenu n'est téléchargé.
    """
    rendered: dict[str, str] = {
        "From": sender,
        "To": recipients,
        "Subject": subject,
        "Date": date,
    }
    if cc is not None:
        rendered["Cc"] = cc

    body_parts: list[dict[str, Any]] = []
    if plain is not None:
        body_parts.append(_gmail_body_part("text/plain", plain))
    if html is not None:
        body_parts.append(_gmail_body_part("text/html", html))
    for name in attachments:
        body_parts.append(
            {
                "partId": f"attachment-{name}",
                "mimeType": "application/octet-stream",
                "filename": name,
                "body": {"attachmentId": f"attachment-id-{name}", "size": 42},
            }
        )

    if len(body_parts) == 1 and not attachments:
        payload: dict[str, Any] = {
            **body_parts[0],
            "headers": [
                {"name": name, "value": value}
                for name, value in rendered.items()
            ],
        }
    else:
        payload = {
            "mimeType": (
                "multipart/mixed" if attachments else "multipart/alternative"
            ),
            "headers": [
                {"name": name, "value": value}
                for name, value in rendered.items()
            ],
            "parts": body_parts,
        }

    return {
        "id": message_id,
        "threadId": thread_id if thread_id is not None else f"thread-{message_id}",
        "payload": payload,
    }


def gmail_read_thread(
    thread_id: str, *messages: Mapping[str, Any]
) -> dict[str, Any]:
    """Objet Gmail rendu par ``GET /threads/{id}?format=full``."""
    return {
        "id": thread_id,
        "historyId": "history-1",
        "messages": [dict(message) for message in messages],
    }


def calendar_event(
    event_id: str,
    *,
    summary: str = "Déjeuner jeudi",
    start: str = "2026-11-12T12:00:00+01:00",
    end: str = "2026-11-12T13:30:00+01:00",
    all_day: bool = False,
    location: str = "Cantine",
    attendees: Sequence[tuple[str, str]] = (),
    status: str = "confirmed",
    html_link: str = "https://calendar.google.com/calendar/event?eid=e1",
) -> dict[str, Any]:
    """Événement d'agenda tel que ``GET /calendar/v3/.../events`` le rend.

    ``all_day`` place la date dans ``start.date`` / ``end.date`` (fin
    exclusive), sinon dans ``start.dateTime`` / ``end.dateTime``. Les
    participants sont des couples ``(courriel, responseStatus)``.
    """
    bound = "date" if all_day else "dateTime"
    return {
        "id": event_id,
        "summary": summary,
        "start": {bound: start},
        "end": {bound: end},
        "location": location,
        "attendees": [
            {"email": email, "responseStatus": response_status}
            for email, response_status in attendees
        ],
        "status": status,
        "htmlLink": html_link,
    }


def calendar_event_list(
    *events: Mapping[str, Any], next_page_token: str | None = None
) -> dict[str, Any]:
    """Objet Calendar rendu par ``GET /calendar/v3/.../events``."""
    body: dict[str, Any] = {"items": list(events)}
    if next_page_token is not None:
        body["nextPageToken"] = next_page_token
    return body


@contextlib.asynccontextmanager
async def google_workspace_session(
    tmp_path: Path,
    stub: Any,
    *,
    sink: RecordingTraceSink | None = None,
    accounts: str = ACCOUNTS,
    default_account: str | None = DEFAULT_ACCOUNT,
    config: Mapping[str, str] | None = None,
    secrets: Mapping[str, str] | None = None,
    constants: Mapping[str, float] | None = None,
) -> AsyncIterator[
    tuple[Any, Any, Any, RecordingTraceSink, RecordingTraceSink]
]:
    """App réelle, Connexion Live, plugin réel branché sur le faux serveur."""
    sink = sink if sink is not None else RecordingTraceSink()
    registry, loader = build_plugins(
        tmp_path,
        sink,
        stub.base_url,
        token_url=stub.token_url,
        accounts=accounts,
        default_account=default_account,
        config=config,
        secrets=secrets,
        constants=constants,
    )
    async with running_app(registry=registry, loader=loader) as (
        app,
        live,
        transport,
        app_sink,
    ):
        async with live_session(transport, live) as session:
            yield app, live, session, sink, app_sink
