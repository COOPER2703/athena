"""Transport des Clients authentifié par Jeton d'accès (spec #75, ticket #79).

Chaque scénario démarre le vrai transport sur un port éphémère avec une vraie
base SQLite temporaire, puis parle le Protocole réel via le client
``websockets``. Le refus est le vrai refus de handshake HTTP : ``401`` avec
``WWW-Authenticate: Bearer``, avant qu'aucune trame ne soit décodée.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import aiohttp
import pytest
import websockets
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from conftest import (
    RecordingTraceSink,
    _url,
    console_session,
    install_console,
    register,
    running_transport,
)
from protocol import Ping, Pong, Register, Registered, decode, encode
from server.adapters.auth import SqliteAuthStore
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
from server.app.secrets import EnvSecretResolver


class _FakeMemory:
    """Mémoire factice : ``build_app`` ne charge ni modèle ni base réelle."""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])



class FakeClock:
    """Horloge réglable : la dernière utilisation n'est écrite qu'une fois par minute."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.value = now

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _store(tmp_path: Path, clock: FakeClock | None = None) -> SqliteAuthStore:
    return SqliteAuthStore(
        db_path=str(tmp_path / "auth.db"),
        trace_sink=RecordingTraceSink(),
        now=clock or FakeClock(),
    )


async def _connect_refused(url: str, *, headers: dict[str, str]) -> InvalidStatus:
    """Ouvre un handshake qui doit être refusé, et rend la réponse HTTP reçue."""
    with pytest.raises(InvalidStatus) as excinfo:
        async with websockets.connect(url, additional_headers=headers):
            pass
    return excinfo.value


def _assert_unauthorized(refusal: InvalidStatus) -> None:
    response: Response = refusal.response
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_handshake_refuses_source_unknown_revoked_and_malformed_tokens(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        source = await store.create_token(name="home", kind="source", source="ha")
        revoked = await store.create_token(name="old desktop", kind="client")
        assert await store.revoke_token(revoked.token.id) is not None

        async with running_transport(auth=store) as transport:
            url = _url(transport)
            for headers in (
                {"Authorization": f"Bearer {source.secret}"},
                {"Authorization": "Bearer ath_unknown_secret"},
                {"Authorization": f"Bearer {revoked.secret}"},
                {"Authorization": ""},
                {"Authorization": "Bearer"},
                {"Authorization": "Basic ath_whatever"},
                {"Authorization": "Bearer two words"},
            ):
                refusal = await _connect_refused(url, headers=headers)
                _assert_unauthorized(refusal)
            assert transport.clients == {}

    asyncio.run(scenario())


def test_a_valid_client_token_registers_and_is_acknowledged(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path)
        issued = await store.create_token(name="desktop", kind="client")

        async with running_transport(sink=sink, auth=store) as transport:
            headers = {"Authorization": f"Bearer {issued.secret}"}
            async with websockets.connect(
                _url(transport), additional_headers=headers
            ) as ws:
                response = await register(ws, transport, client_name="desktop")
                assert isinstance(response, Registered)
                assert response.client_id in transport.clients
            assert sink.of_kind("client_authorized")
            assert not sink.of_kind("handshake_refused")

    asyncio.run(scenario())


def test_last_use_is_recorded_at_the_handshake_not_per_frame(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock(1_000_000.0)
        store = _store(tmp_path, clock)
        issued = await store.create_token(name="desktop", kind="client")
        headers = {"Authorization": f"Bearer {issued.secret}"}

        async with running_transport(auth=store) as transport:
            async with websockets.connect(
                _url(transport), additional_headers=headers
            ) as ws:
                await register(ws, transport)
                first_use = clock.value
                assert (await store.list_tokens())[0].last_used_at == first_use

                # Les trames ne sont pas des utilisations : même après plus
                # d'une minute, la dernière utilisation ne bouge pas.
                clock.advance(120.0)
                await ws.send(encode(Ping()))
                assert isinstance(
                    decode(await asyncio.wait_for(ws.recv(), 1.0)), Pong
                )
                assert (await store.list_tokens())[0].last_used_at == first_use

            # Le handshake suivant, lui, enregistre la nouvelle utilisation.
            async with websockets.connect(
                _url(transport), additional_headers=headers
            ):
                pass
            assert (await store.list_tokens())[0].last_used_at == clock.value

    asyncio.run(scenario())


def test_revocation_takes_effect_on_the_next_handshake(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        issued = await store.create_token(name="desktop", kind="client")
        headers = {"Authorization": f"Bearer {issued.secret}"}

        async with running_transport(auth=store) as transport:
            async with websockets.connect(
                _url(transport), additional_headers=headers
            ) as ws:
                await register(ws, transport)

            assert await store.revoke_token(issued.token.id) is not None

            refusal = await _connect_refused(_url(transport), headers=headers)
            _assert_unauthorized(refusal)

    asyncio.run(scenario())


def test_build_app_shares_one_auth_store_between_console_and_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La révocation depuis la Console vaut pour le transport, sans redémarrage."""

    async def scenario() -> None:
        monkeypatch.setattr(
            "server.app.root.SentenceTransformerEmbedder", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "server.app.root.SqliteMemoryStore", lambda *a, **k: _FakeMemory()
        )
        console_port = _free_port()
        transport_port = _free_port()
        config = AppConfig(
            gemini=GeminiConfig(api_key="test-key"),
            server=ServerConfig(host="127.0.0.1", port=transport_port),
            console=ConsoleConfig(port=console_port),
            webhook=WebhookConfig(enabled=False),
            plugins=PluginsConfig(
                directory=str(tmp_path / "plugins"),
                state_path=str(tmp_path / "plugins.json"),
            ),
            log_dir=str(tmp_path),
            auth=AuthConfig(db_path=str(tmp_path / "auth.db")),
        )
        app = build_app(config, secrets=EnvSecretResolver({}), env={}, env_file=None)
        runner = asyncio.create_task(app.run())
        base_url = f"http://127.0.0.1:{console_port}"
        ws_url = f"ws://127.0.0.1:{transport_port}"
        try:
            async with console_session(base_url) as http:
                await install_console(http, base_url, tmp_path / "server.log")
                async with http.post(
                    f"{base_url}/api/tokens",
                    json={"name": "desktop", "kind": "client"},
                ) as response:
                    assert response.status == 201, await response.text()
                    created = await response.json()
                secret = created["secret"]
                token_id = created["token"]["id"]

                headers = {"Authorization": f"Bearer {secret}"}
                async with websockets.connect(
                    ws_url, additional_headers=headers
                ) as ws:
                    await ws.send(encode(Register(client_name="desktop")))
                    ack = decode(await asyncio.wait_for(ws.recv(), 1.0))
                    assert isinstance(ack, Registered)

                # La Console lit la dernière utilisation écrite par le
                # transport : les deux surfaces partagent le même store.
                async with http.get(f"{base_url}/api/tokens") as response:
                    assert response.status == 200
                    listed = await response.json()
                assert listed["tokens"][0]["last_used_at"] is not None

                async with http.delete(
                    f"{base_url}/api/tokens/{token_id}"
                ) as response:
                    assert response.status == 200

            with pytest.raises(InvalidStatus) as excinfo:
                async with websockets.connect(
                    ws_url, additional_headers=headers
                ):
                    pass
            _assert_unauthorized(excinfo.value)
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)

    asyncio.run(scenario())


def test_handshake_without_authorization_is_refused_before_any_frame(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        store = _store(tmp_path)
        async with running_transport(sink=sink, auth=store) as transport:
            refusal = await _connect_refused(_url(transport), headers={})
            _assert_unauthorized(refusal)

            # Rien n'a été décodé ni enregistré : la connexion n'a jamais existé.
            assert transport.clients == {}
            assert sink.of_kind("client_registered") == []
            assert sink.of_kind("protocol_error") == []
            assert sink.of_kind("invalid_frame") == []

    asyncio.run(scenario())
