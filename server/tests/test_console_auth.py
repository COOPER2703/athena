"""Contrat HTTP d'authentification de la Console, contre un vrai serveur.

Chaque scénario démarre l'adaptateur sur un port éphémère avec une vraie base
SQLite temporaire, puis l'interroge avec un client HTTP réel : statut, corps
JSON, attributs du ``Set-Cookie``, refus de l'upgrade WebSocket. Aucun module
n'est simulé — c'est la surface observable par un navigateur qui est testée.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Mapping

import aiohttp
import pytest

from conftest import RecordingTraceSink
from server.adapters.auth import SqliteAuthStore
from server.adapters.console import ConsoleAdapter
from server.adapters.console.contract import (
    EVENTS_PATH,
    HEALTH_PATH,
    LOGIN_PATH,
    LOGOUT_ALL_PATH,
    LOGOUT_PATH,
    PASSWORD_PATH,
    SESSION_PATH,
    SETUP_PATH,
    STATUS_PATH,
    STREAM_PATH,
    TICKET_COOKIE,
)

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "an entirely different passphrase"
WEAK_PASSWORD = "short"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.value = now

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _snapshot() -> Mapping[str, object]:
    return {
        "session": {
            "state": "SessionVisible",
            "session_id": "sess-1",
            "connection_id": "s1",
            "active_client_id": "client-1",
        },
        "clients": [],
        "plugins": [],
        "config": {"version": "0.1.0"},
    }


class Console:
    """Console démarrée avec une vraie base d'auth et un tampon de traces vide."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        clock: FakeClock | None = None,
        snapshot=_snapshot,
        static_dir: Path | None = None,
        **store_options: object,
    ) -> None:
        self.clock = clock or FakeClock()
        self.snapshot = snapshot
        self.store = SqliteAuthStore(
            db_path=str(tmp_path / "auth.db"),
            trace_sink=RecordingTraceSink(),
            now=self.clock,
            **store_options,
        )
        self.adapter = ConsoleAdapter(
            port=0,
            auth=self.store,
            static_dir=static_dir or (tmp_path / "missing-build"),
        )
        self.pushed: list[tuple[str, str]] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.adapter.port}"

    async def start(self) -> None:
        await self.adapter.start(
            self.snapshot,
            lambda source, content: self.pushed.append((source, content)),
        )

    async def stop(self) -> None:
        await self.adapter.stop()

    def client(self, *, cookies: bool = True) -> aiohttp.ClientSession:
        jar = (
            aiohttp.CookieJar(unsafe=True, treat_as_secure_origin=self.url)
            if cookies
            else aiohttp.DummyCookieJar()
        )
        return aiohttp.ClientSession(cookie_jar=jar)

    def install_code(self) -> str:
        code = self.store.announce_installation_code()
        assert code is not None
        return code


async def _json(response: aiohttp.ClientResponse) -> dict[str, object]:
    assert response.content_type == "application/json"
    return await response.json()


def _set_cookies(response: aiohttp.ClientResponse) -> list[str]:
    return response.headers.getall("Set-Cookie", [])


async def _install(
    console: Console, client: aiohttp.ClientSession, *, username: str = "admin"
) -> aiohttp.ClientResponse:
    return await client.post(
        f"{console.url}{SETUP_PATH}",
        json={"code": console.install_code(), "username": username, "password": PASSWORD},
    )


def test_a_fresh_instance_answers_setup_required_before_anything_else(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                for method, path in (
                    ("GET", STATUS_PATH),
                    ("GET", "/api/tokens"),
                    ("GET", "/api/unknown"),
                    ("POST", EVENTS_PATH),
                    ("GET", STREAM_PATH),
                ):
                    async with http.request(
                        method,
                        f"{console.url}{path}",
                        headers={"Upgrade": "websocket"} if path == STREAM_PATH else {},
                    ) as response:
                        assert response.status == 409, path
                        assert (await _json(response))["error"] == "setup_required"

                async with http.get(f"{console.url}{SESSION_PATH}") as response:
                    assert response.status == 200
                    assert await _json(response) == {
                        "v": 1,
                        "setup_required": True,
                        "authenticated": False,
                        "username": None,
                    }

                async with http.get(f"{console.url}{SETUP_PATH}") as response:
                    assert response.status == 200
                    assert await _json(response) == {"v": 1, "setup_required": True}

                async with http.get(f"{console.url}/") as response:
                    assert response.status == 200
                    assert "text/html" in response.headers["Content-Type"]
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_install_with_a_wrong_code_is_forbidden(tmp_path: Path) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                async with http.post(
                    f"{console.url}{SETUP_PATH}",
                    json={
                        "code": "not-the-code",
                        "username": "admin",
                        "password": PASSWORD,
                    },
                ) as response:
                    assert response.status == 403
                    assert (await _json(response))["error"] == "invalid_code"

                async with http.get(f"{console.url}{SESSION_PATH}") as response:
                    assert (await _json(response))["setup_required"] is True
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_install_opens_an_httponly_secure_ticket_and_unlocks_the_api(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200
                    assert await _json(response) == {
                        "v": 1,
                        "authenticated": True,
                        "username": "admin",
                    }
                    [cookie] = _set_cookies(response)
                    assert cookie.startswith(f"{TICKET_COOKIE}=")
                    assert "HttpOnly" in cookie
                    assert "Secure" in cookie
                    assert "SameSite=Strict" in cookie
                    assert "Path=/" in cookie
                    assert "Max-Age=2592000" in cookie

                async with http.get(f"{console.url}{STATUS_PATH}") as response:
                    assert response.status == 200
                    assert await _json(response) == {"v": 1, **_snapshot()}

                async with http.get(f"{console.url}{SESSION_PATH}") as response:
                    assert await _json(response) == {
                        "v": 1,
                        "setup_required": False,
                        "authenticated": True,
                        "username": "admin",
                    }
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_a_second_install_is_refused_once_the_account_exists(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            code = console.install_code()
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200

            # Client distinct, sans cookie : l'écran d'installation ne doit pas
            # rouvrir une porte dérobée.
            async with console.client() as http:
                async with http.post(
                    f"{console.url}{SETUP_PATH}",
                    json={"code": code, "username": "intruder", "password": PASSWORD},
                ) as response:
                    assert response.status == 409
                    assert (await _json(response))["error"] == "already_installed"

                async with http.get(f"{console.url}{SETUP_PATH}") as response:
                    assert (await _json(response))["setup_required"] is False
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_the_minimum_password_length_is_enforced_by_the_server(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                code = console.install_code()
                async with http.post(
                    f"{console.url}{SETUP_PATH}",
                    json={"code": code, "username": "admin", "password": WEAK_PASSWORD},
                ) as response:
                    assert response.status == 400
                    body = await _json(response)
                    assert body["error"] == "weak_password"
                    assert "12" in str(body["message"])

                async with http.get(f"{console.url}{SESSION_PATH}") as response:
                    assert (await _json(response))["setup_required"] is True
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_login_opens_a_ticket_and_failures_do_not_discriminate(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200

            async with console.client() as http:
                async with http.post(
                    f"{console.url}{LOGIN_PATH}",
                    json={"username": "admin", "password": "wrong password"},
                ) as response:
                    assert response.status == 401
                    wrong_password = await _json(response)

                async with http.post(
                    f"{console.url}{LOGIN_PATH}",
                    json={"username": "nobody", "password": PASSWORD},
                ) as response:
                    assert response.status == 401
                    assert await _json(response) == wrong_password

                async with http.post(
                    f"{console.url}{LOGIN_PATH}",
                    json={"username": "admin", "password": PASSWORD},
                ) as response:
                    assert response.status == 200
                    assert await _json(response) == {
                        "v": 1,
                        "authenticated": True,
                        "username": "admin",
                    }
                    assert _set_cookies(response)

                async with http.get(f"{console.url}{STATUS_PATH}") as response:
                    assert response.status == 200
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_five_failures_lock_the_login_with_a_retry_after_then_lift(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        console = Console(tmp_path, clock=clock)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200

            async with console.client() as http:
                for _ in range(4):
                    async with http.post(
                        f"{console.url}{LOGIN_PATH}",
                        json={"username": "admin", "password": "wrong"},
                    ) as response:
                        assert response.status == 401

                async with http.post(
                    f"{console.url}{LOGIN_PATH}",
                    json={"username": "admin", "password": "wrong"},
                ) as response:
                    assert response.status == 429
                    assert response.headers["Retry-After"] == "900"
                    assert (await _json(response))["error"] == "too_many_attempts"

                clock.advance(15 * 60)

                async with http.post(
                    f"{console.url}{LOGIN_PATH}",
                    json={"username": "admin", "password": PASSWORD},
                ) as response:
                    assert response.status == 200
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_every_api_route_without_a_ticket_answers_json_401(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        static = tmp_path / "build"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><div id=root>")
        (static / "app.js").write_text("console.log('athena')")
        console = Console(tmp_path, static_dir=static)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200

            async with console.client() as http:
                for path in (
                    STATUS_PATH,
                    "/api/tokens",
                    "/api/unknown",
                    "/api",
                ):
                    async with http.get(f"{console.url}{path}") as response:
                        assert response.status == 401, path
                        assert (await _json(response))["error"] == "unauthorized"

                async with http.post(
                    f"{console.url}{EVENTS_PATH}",
                    json={"source": "console", "content": "coucou"},
                ) as response:
                    assert response.status == 401
                    assert (await _json(response))["error"] == "unauthorized"

                async with http.get(f"{console.url}/") as response:
                    assert response.status == 200
                    assert "id=root" in await response.text()

                async with http.get(f"{console.url}/app.js") as response:
                    assert response.status == 200
                    assert "athena" in await response.text()

                async with http.get(f"{console.url}/some/route") as response:
                    assert response.status == 200
                    assert "id=root" in await response.text()
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_the_stream_upgrade_is_refused_without_a_ticket(tmp_path: Path) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200

            # Autre navigateur, sans cookie : le handshake est refusé en HTTP
            # avant tout ``101 Switching Protocols``.
            async with console.client() as http:
                with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
                    await http.ws_connect(f"{console.url}{STREAM_PATH}")
                assert excinfo.value.status == 401

            # Avec le Ticket Console, le flux s'ouvre normalement.
            async with console.client() as http:
                await _login_session(console, http)
                ws = await http.ws_connect(f"{console.url}{STREAM_PATH}")
                hello = await asyncio.wait_for(ws.receive_json(), 1.0)
                assert hello["type"] == "hello"
                await ws.close()
        finally:
            await console.stop()

    asyncio.run(scenario())


async def _login_session(
    console: Console, http: aiohttp.ClientSession
) -> None:
    """Connecte le client : le jar conserve le Ticket Console."""
    async with http.post(
        f"{console.url}{LOGIN_PATH}",
        json={"username": "admin", "password": PASSWORD},
    ) as response:
        assert response.status == 200


def test_healthz_is_public_and_never_reads_the_snapshot(tmp_path: Path) -> None:
    reads: list[int] = []

    def counted_snapshot() -> Mapping[str, object]:
        reads.append(1)
        return _snapshot()

    async def scenario() -> None:
        console = Console(tmp_path, snapshot=counted_snapshot)
        await console.start()
        try:
            async with console.client() as http:
                before = len(reads)
                async with http.get(f"{console.url}{HEALTH_PATH}") as response:
                    assert response.status == 200
                    assert await _json(response) == {"status": "ok"}
                assert len(reads) == before
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_a_foreign_origin_is_refused_on_mutating_requests(tmp_path: Path) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200

                async with http.post(
                    f"{console.url}{EVENTS_PATH}",
                    json={"source": "console", "content": "depuis ailleurs"},
                    headers={"Origin": "http://evil.example"},
                ) as response:
                    assert response.status == 403
                    assert (await _json(response))["error"] == "origin_refused"

                async with http.post(
                    f"{console.url}{EVENTS_PATH}",
                    json={"source": "console", "content": "depuis la console"},
                    headers={"Origin": console.url},
                ) as response:
                    assert response.status == 202
                    assert console.pushed == [("console", "depuis la console")]

                # Une lecture n'est pas une requête mutante : l'Origin étrangère
                # ne la bloque pas.
                async with http.get(
                    f"{console.url}{STATUS_PATH}",
                    headers={"Origin": "http://evil.example"},
                ) as response:
                    assert response.status == 200
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_the_ticket_cookie_is_reissued_only_when_the_expiry_slides(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = FakeClock()
        console = Console(tmp_path, clock=clock)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200

                async with http.get(f"{console.url}{STATUS_PATH}") as response:
                    assert response.status == 200
                    assert _set_cookies(response) == []

                clock.advance(60 * 60 + 1)

                async with http.get(f"{console.url}{STATUS_PATH}") as response:
                    assert response.status == 200
                    [cookie] = _set_cookies(response)
                    assert "Max-Age=2592000" in cookie
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_a_revoked_ticket_is_refused_on_the_very_next_request(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200
                    [cookie] = _set_cookies(response)

                secret = cookie.split(";", 1)[0].split("=", 1)[1]
                await console.store.revoke_ticket(secret)

                async with http.get(f"{console.url}{STATUS_PATH}") as response:
                    assert response.status == 401
                    assert (await _json(response))["error"] == "unauthorized"
        finally:
            await console.stop()

    asyncio.run(scenario())


def _ticket_secret(response: aiohttp.ClientResponse) -> str:
    """Secret porté par le ``Set-Cookie`` d'installation ou de connexion."""
    [cookie] = _set_cookies(response)
    return cookie.split(";", 1)[0].split("=", 1)[1]


async def _login(
    console: Console,
    http: aiohttp.ClientSession,
    password: str = PASSWORD,
) -> aiohttp.ClientResponse:
    return await http.post(
        f"{console.url}{LOGIN_PATH}",
        json={"username": "admin", "password": password},
    )


def test_changing_the_password_requires_the_current_one(tmp_path: Path) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200

                # Sans le mot de passe actuel — vide ou faux — le changement est
                # refusé, et surtout le mot de passe reste inchangé.
                for current in ("", "not the current password"):
                    async with http.post(
                        f"{console.url}{PASSWORD_PATH}",
                        json={
                            "current_password": current,
                            "new_password": NEW_PASSWORD,
                        },
                    ) as response:
                        assert response.status == 403
                        assert (await _json(response))["error"] == "invalid_password"

                # Le nouveau mot de passe subit la même longueur minimale que
                # celui de l'installation.
                async with http.post(
                    f"{console.url}{PASSWORD_PATH}",
                    json={
                        "current_password": PASSWORD,
                        "new_password": WEAK_PASSWORD,
                    },
                ) as response:
                    assert response.status == 400
                    assert (await _json(response))["error"] == "weak_password"

                async with console.client() as other:
                    async with await _login(console, other) as response:
                        assert response.status == 200

                async with console.client() as other:
                    async with await _login(console, other, NEW_PASSWORD) as response:
                        assert response.status == 401
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_changing_the_password_switches_it_and_revokes_other_tickets(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as current:
                async with await _install(console, current) as response:
                    assert response.status == 200

                async with console.client() as other:
                    async with await _login(console, other) as response:
                        assert response.status == 200
                        other_secret = _ticket_secret(response)

                    async with current.post(
                        f"{console.url}{PASSWORD_PATH}",
                        json={
                            "current_password": PASSWORD,
                            "new_password": NEW_PASSWORD,
                        },
                    ) as response:
                        assert response.status == 200
                        assert await _json(response) == {"v": 1, "changed": True}

                    # Le Ticket courant survit au changement...
                    async with current.get(f"{console.url}{STATUS_PATH}") as response:
                        assert response.status == 200

                    # ...les autres Tickets Console du Compte sont révoqués
                    # immédiatement.
                    async with other.get(f"{console.url}{STATUS_PATH}") as response:
                        assert response.status == 401
                        assert (await _json(response))["error"] == "unauthorized"

            async with console.client(cookies=False) as replay:
                async with replay.get(
                    f"{console.url}{STATUS_PATH}",
                    cookies={TICKET_COOKIE: other_secret},
                ) as response:
                    assert response.status == 401

            # L'ancien mot de passe est refusé, le nouveau accepté.
            async with console.client() as fresh:
                async with await _login(console, fresh) as response:
                    assert response.status == 401
                    assert (await _json(response))["error"] == "invalid_credentials"

            async with console.client() as fresh:
                async with await _login(console, fresh, NEW_PASSWORD) as response:
                    assert response.status == 200
                    assert _set_cookies(response)
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_logout_revokes_the_current_ticket_and_clears_the_cookie(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                async with await _install(console, http) as response:
                    assert response.status == 200
                    secret = _ticket_secret(response)

                async with http.post(f"{console.url}{LOGOUT_PATH}") as response:
                    assert response.status == 200
                    assert await _json(response) == {"v": 1, "authenticated": False}
                    [cookie] = _set_cookies(response)
                    assert cookie.startswith(f"{TICKET_COOKIE}=")
                    assert "Max-Age=0" in cookie
                    assert "expires=Thu, 01 Jan 1970" in cookie

            # Rejouer le secret révoqué ne suffit pas : le Ticket est mort, pas
            # seulement absent du navigateur.
            async with console.client(cookies=False) as replay:
                async with replay.get(
                    f"{console.url}{STATUS_PATH}",
                    cookies={TICKET_COOKIE: secret},
                ) as response:
                    assert response.status == 401

            async with console.client() as fresh:
                async with await _login(console, fresh) as response:
                    assert response.status == 200
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_logout_all_revokes_every_ticket_of_the_account(tmp_path: Path) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as first, console.client() as second:
                async with await _install(console, first) as response:
                    assert response.status == 200
                    first_secret = _ticket_secret(response)
                async with await _login(console, second) as response:
                    assert response.status == 200

                async with first.post(f"{console.url}{LOGOUT_ALL_PATH}") as response:
                    assert response.status == 200
                    assert await _json(response) == {"v": 1, "authenticated": False}
                    [cookie] = _set_cookies(response)
                    assert cookie.startswith(f"{TICKET_COOKIE}=")
                    assert "Max-Age=0" in cookie
                    assert "expires=Thu, 01 Jan 1970" in cookie

                async with second.get(f"{console.url}{STATUS_PATH}") as response:
                    assert response.status == 401

                async with console.client(cookies=False) as replay:
                    async with replay.get(
                        f"{console.url}{STATUS_PATH}",
                        cookies={TICKET_COOKIE: first_secret},
                    ) as response:
                        assert response.status == 401

            # Le Compte existe toujours : « déconnecter partout » ne change pas
            # le mot de passe.
            async with console.client() as fresh:
                async with await _login(console, fresh) as response:
                    assert response.status == 200
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_a_revoked_ticket_cannot_upgrade_the_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as first, console.client() as second:
                async with await _install(console, first) as response:
                    assert response.status == 200
                async with await _login(console, second) as response:
                    assert response.status == 200

                # Le flux s'ouvre avec un Ticket valide...
                ws = await second.ws_connect(f"{console.url}{STREAM_PATH}")
                await ws.close()

                async with first.post(f"{console.url}{LOGOUT_ALL_PATH}") as response:
                    assert response.status == 200

                # ...et le handshake suivant est refusé avant tout upgrade.
                with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
                    await second.ws_connect(f"{console.url}{STREAM_PATH}")
                assert excinfo.value.status == 401
        finally:
            await console.stop()

    asyncio.run(scenario())
