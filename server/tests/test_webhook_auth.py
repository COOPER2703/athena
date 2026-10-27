"""Auth du Webhook : Jeton d'accès de nature ``source``, contre un vrai serveur.

Chaque scénario démarre l'adaptateur sur un port éphémère avec une vraie base
SQLite temporaire et un Jeton créé par le store réel. Aucun module n'est
simulé : c'est la surface observable par un intégrateur qui est testée.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

import aiohttp

from conftest import (
    FakeClock,
    RecordingTraceSink,
    console_session,
    install_console,
)
from server.adapters.auth import SqliteAuthStore
from server.adapters.webhook import (
    EVENTS_PATH,
    MAX_CONTENT_CHARS,
    WebhookAdapter,
)
from server.app.config import (
    AppConfig,
    AuthConfig,
    ConsoleConfig,
    GeminiConfig,
    PluginsConfig,
    ServerConfig,
    WebhookConfig,
)
from server.app.root import build_app

SOURCE = "matrix"
CONTENT = "porte ouverte"


class _FakeMemory:
    """``build_app`` ne charge ni modèle ni base réelle dans ces scénarios."""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Recorder:
    """Couture ``push`` de test : enregistre les Événements reçus."""

    def __init__(self) -> None:
        self.pushes: list[tuple[str, str]] = []

    def __call__(self, source: str, content: str) -> None:
        self.pushes.append((source, content))


class Webhook:
    """Webhook démarré avec une vraie base d'auth et un vrai store."""

    def __init__(self, tmp_path: Path, *, clock: FakeClock | None = None) -> None:
        self.clock = clock or FakeClock()
        self.sink = RecordingTraceSink()
        self.store = SqliteAuthStore(
            db_path=str(tmp_path / "auth.db"),
            trace_sink=RecordingTraceSink(),
            now=self.clock,
        )
        self.adapter = WebhookAdapter(
            port=0, trace_sink=self.sink, auth=self.store
        )
        self.recorder = Recorder()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.adapter.port}{EVENTS_PATH}"

    async def start(self) -> None:
        await self.adapter.start(self.recorder)

    async def stop(self) -> None:
        await self.adapter.stop()

    async def source_token(self, source: str = SOURCE) -> str:
        issued = await self.store.create_token(
            name="Matrix", kind="source", source=source
        )
        return issued.secret

    @staticmethod
    def bearer(secret: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}"}


def test_a_request_without_authorization_header_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    webhook.url, json={"content": CONTENT}
                ) as response:
                    assert response.status == 401
                    assert response.headers["WWW-Authenticate"] == "Bearer"
                    assert "error" in await response.json()
            assert webhook.recorder.pushes == []
            assert webhook.sink.of_kind("event_received") == []
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_the_refusal_happens_before_the_body_is_parsed(tmp_path: Path) -> None:
    """Un inconnu n'obtient jamais un diagnostic sur son corps de requête."""

    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    webhook.url,
                    data="{not json",
                    headers={"Content-Type": "application/json"},
                ) as response:
                    assert response.status == 401
            assert webhook.recorder.pushes == []
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_a_malformed_authorization_header_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            async with aiohttp.ClientSession() as http:
                for header in (
                    "",
                    "Bearer",
                    "Bearer   ",
                    "Basic YWRtaW46c2VjcmV0",
                    "ath_secret_sans_schema",
                ):
                    async with http.post(
                        webhook.url,
                        json={"content": CONTENT},
                        headers={"Authorization": header},
                    ) as response:
                        assert response.status == 401, header
            assert webhook.recorder.pushes == []
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_an_unknown_token_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    webhook.url,
                    json={"content": CONTENT},
                    headers=webhook.bearer("ath_inconnu"),
                ) as response:
                    assert response.status == 401
            assert webhook.recorder.pushes == []
            assert webhook.sink.of_kind("event_received") == []
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_a_client_kind_token_cannot_open_the_webhook(tmp_path: Path) -> None:
    """Un Jeton du transport ne vaut pas pour la surface des Sources."""

    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            issued = await webhook.store.create_token(
                name="Portable", kind="client"
            )
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    webhook.url,
                    json={"content": CONTENT},
                    headers=webhook.bearer(issued.secret),
                ) as response:
                    assert response.status == 401
            assert webhook.recorder.pushes == []
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_a_revoked_token_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            issued = await webhook.store.create_token(
                name="Matrix", kind="source", source=SOURCE
            )
            await webhook.store.revoke_token(issued.token.id)
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    webhook.url,
                    json={"content": CONTENT},
                    headers=webhook.bearer(issued.secret),
                ) as response:
                    assert response.status == 401
            assert webhook.recorder.pushes == []
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_a_valid_source_token_is_accepted_and_the_token_source_wins(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            secret = await webhook.source_token()
            async with aiohttp.ClientSession() as http:
                # Sans source dans le corps : c'est celle du Jeton qui fait foi.
                async with http.post(
                    webhook.url,
                    json={"content": CONTENT},
                    headers=webhook.bearer(secret),
                ) as response:
                    assert response.status == 202
                    assert await response.json() == {"status": "accepted"}
            assert webhook.recorder.pushes == [(SOURCE, CONTENT)]
            [entry] = webhook.sink.of_kind("event_received")
            assert entry.source == "webhook"
            assert entry.payload == {"source": SOURCE, "chars": len(CONTENT)}
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_a_body_source_equal_to_the_token_source_is_accepted(
    tmp_path: Path,
) -> None:
    """Le champ ``source`` du corps reste accepté pour compatibilité."""

    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            secret = await webhook.source_token()
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    webhook.url,
                    json={"source": SOURCE, "content": CONTENT},
                    headers=webhook.bearer(secret),
                ) as response:
                    assert response.status == 202
            assert webhook.recorder.pushes == [(SOURCE, CONTENT)]
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_a_contradictory_body_source_is_refused_without_pushing(
    tmp_path: Path,
) -> None:
    """L'usurpation de Source est rendue visible, pas silencieusement corrigée."""

    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            secret = await webhook.source_token()
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    webhook.url,
                    json={"source": "home-assistant", "content": CONTENT},
                    headers=webhook.bearer(secret),
                ) as response:
                    assert response.status == 403
                    assert await response.json() == {
                        "error": "source does not match the token source"
                    }
            assert webhook.recorder.pushes == []
            assert webhook.sink.of_kind("event_received") == []
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_the_last_use_is_recorded_at_most_once_per_minute(tmp_path: Path) -> None:
    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            secret = await webhook.source_token()
            headers = webhook.bearer(secret)

            async def post() -> None:
                async with aiohttp.ClientSession() as http:
                    async with http.post(
                        webhook.url, json={"content": CONTENT}, headers=headers
                    ) as response:
                        assert response.status == 202

            async def last_used() -> float | None:
                [token] = await webhook.store.list_tokens()
                return token.last_used_at

            await post()
            first = await last_used()
            assert first == webhook.clock.value

            webhook.clock.advance(30)
            await post()
            assert await last_used() == first

            webhook.clock.advance(31)
            await post()
            assert await last_used() == webhook.clock.value
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_the_event_body_rules_are_unchanged_for_an_authenticated_caller(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        webhook = Webhook(tmp_path)
        await webhook.start()
        try:
            secret = await webhook.source_token()
            headers = webhook.bearer(secret)
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    webhook.url,
                    json={"content": "x" * (MAX_CONTENT_CHARS + 1)},
                    headers=headers,
                ) as response:
                    assert response.status == 413
                async with http.post(
                    webhook.url,
                    data='{"content": "porte ouverte"}',
                    headers={**headers, "Content-Type": "text/plain"},
                ) as response:
                    assert response.status == 415
            assert webhook.recorder.pushes == []
            assert webhook.sink.of_kind("event_received") == []
        finally:
            await webhook.stop()

    asyncio.run(scenario())


def test_build_app_shares_one_auth_store_between_console_and_webhook(
    tmp_path: Path, monkeypatch
) -> None:
    """La révocation faite dans la Console ferme le Webhook sans redémarrage."""

    async def scenario() -> None:
        monkeypatch.setattr(
            "server.app.root.SentenceTransformerEmbedder", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "server.app.root.SqliteMemoryStore", lambda *a, **k: _FakeMemory()
        )
        console_port = _free_port()
        webhook_port = _free_port()
        config = AppConfig(
            gemini=GeminiConfig(api_key="test-key"),
            server=ServerConfig(host="127.0.0.1", port=_free_port()),
            console=ConsoleConfig(port=console_port),
            webhook=WebhookConfig(enabled=True, port=webhook_port),
            auth=AuthConfig(db_path=str(tmp_path / "auth.db")),
            plugins=PluginsConfig(
                directory=str(tmp_path / "plugins"),
                state_path=str(tmp_path / "plugins.json"),
            ),
            log_dir=str(tmp_path / "logs"),
        )
        root_logger = logging.getLogger()
        handlers = root_logger.handlers[:]
        level = root_logger.level
        app = build_app(config, secrets=None, env={}, env_file=None)
        runner = asyncio.create_task(app.run())
        console_url = f"http://127.0.0.1:{console_port}"
        webhook_url = f"http://127.0.0.1:{webhook_port}{EVENTS_PATH}"
        try:
            async with console_session(console_url) as http:
                await install_console(
                    http, console_url, tmp_path / "logs" / "server.log"
                )
                async with http.post(
                    f"{console_url}/api/tokens",
                    json={
                        "name": "Matrix",
                        "kind": "source",
                        "source": SOURCE,
                    },
                ) as response:
                    assert response.status == 201
                    issued = await response.json()
                secret = issued["secret"]
                token_id = issued["token"]["id"]

                async with aiohttp.ClientSession() as client:
                    # Sans Jeton, la production est fermée.
                    async with client.post(
                        webhook_url, json={"content": CONTENT}
                    ) as response:
                        assert response.status == 401

                    bearer = {"Authorization": f"Bearer {secret}"}
                    async with client.post(
                        webhook_url, json={"content": CONTENT}, headers=bearer
                    ) as response:
                        assert response.status == 202

                    async with client.post(
                        webhook_url,
                        json={"source": "home-assistant", "content": CONTENT},
                        headers=bearer,
                    ) as response:
                        assert response.status == 403

                # La Console révoque : le même store refuse le Jeton au Webhook.
                async with http.delete(
                    f"{console_url}/api/tokens/{token_id}"
                ) as response:
                    assert response.status == 200

                async with aiohttp.ClientSession() as client:
                    async with client.post(
                        webhook_url,
                        json={"content": CONTENT},
                        headers={"Authorization": f"Bearer {secret}"},
                    ) as response:
                        assert response.status == 401
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 5.0)
            root_logger.handlers[:] = handlers
            root_logger.setLevel(level)

    asyncio.run(scenario())
