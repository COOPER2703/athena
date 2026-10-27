"""Contrat HTTP des Jetons d'accès, contre un vrai serveur.

Chaque scénario démarre l'adaptateur Console sur un port éphémère avec une vraie
base SQLite temporaire, ouvre un Ticket Console par le vrai écran d'installation
puis interroge les routes de Jetons avec un client HTTP réel. Aucun module n'est
simulé : c'est la surface observable par le navigateur qui est testée.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import aiohttp

from conftest import (
    Console,
    FakeClock,
    TEST_PASSWORD,
)
from server.adapters.console.contract import (
    LOGIN_PATH,
    TOKENS_PATH,
)


async def _json(response: aiohttp.ClientResponse) -> dict[str, object]:
    assert response.content_type == "application/json"
    return await response.json()


async def _create(
    console: Console,
    http: aiohttp.ClientSession,
    **draft: object,
) -> tuple[int, dict[str, object]]:
    async with http.post(f"{console.url}{TOKENS_PATH}", json=draft) as response:
        return response.status, await _json(response)


def test_every_token_route_requires_a_console_ticket(tmp_path: Path) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                await console.install(http)
                issued = await console.store.create_token(
                    name="Portable", kind="client"
                )

            # Autre navigateur, sans cookie : aucune route de Jetons n'est
            # utilisable, même avec un Jeton d'accès valide en Authorization —
            # les deux mécanismes de credential ne se remplacent pas.
            async with console.client() as http:
                bearer = {"Authorization": f"Bearer {issued.secret}"}
                for method, path, kwargs in (
                    ("GET", TOKENS_PATH, {}),
                    ("POST", TOKENS_PATH, {"json": {"name": "X", "kind": "client"}}),
                    ("DELETE", f"{TOKENS_PATH}/{issued.token.id}", {}),
                ):
                    async with http.request(
                        method, f"{console.url}{path}", headers=bearer, **kwargs
                    ) as response:
                        assert response.status == 401, path
                        assert (await _json(response))["error"] == "unauthorized"

            # Le Ticket Console, lui, ouvre les mêmes routes.
            async with console.client() as http:
                async with http.post(
                    f"{console.url}{LOGIN_PATH}",
                    json={"username": "admin", "password": TEST_PASSWORD},
                ) as response:
                    assert response.status == 200
                async with http.get(f"{console.url}{TOKENS_PATH}") as response:
                    assert response.status == 200
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_creation_returns_the_secret_once_and_the_list_never_reveals_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                await console.install(http)

                status, body = await _create(
                    console, http, name="Portable", kind="client"
                )
                assert status == 201
                assert body["v"] == 1
                assert set(body) == {"v", "token", "secret"}
                secret = body["secret"]
                assert isinstance(secret, str)
                assert secret.startswith("ath_")
                token = body["token"]
                assert token == {
                    "id": token["id"],
                    "name": "Portable",
                    "kind": "client",
                    "source": None,
                    "prefix": secret[: len(token["prefix"])],
                    "created_at": console.clock.value,
                    "last_used_at": None,
                    "revoked_at": None,
                }

                # La liste montre le Jeton, jamais le secret ni son empreinte.
                async with http.get(f"{console.url}{TOKENS_PATH}") as response:
                    assert response.status == 200
                    listing = await _json(response)
                    raw = await response.text()
                assert listing == {
                    "v": 1,
                    "tokens": [token],
                }
                assert set(listing) == {"v", "tokens"}
                assert secret not in raw
                assert hashlib.sha256(secret.encode()).hexdigest() not in raw
                assert token["prefix"] in raw
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_a_source_token_requires_a_source_and_a_client_token_forbids_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                await console.install(http)

                for draft in (
                    {"name": "Esp32", "kind": "source"},
                    {"name": "Esp32", "kind": "source", "source": "   "},
                    {"name": "Portable", "kind": "client", "source": "matrix"},
                ):
                    status, body = await _create(console, http, **draft)
                    assert status == 400, draft
                    assert body["error"] == "invalid_source"

                status, body = await _create(
                    console, http, name="Curieux", kind="admin"
                )
                assert status == 400
                assert body["error"] == "invalid_kind"

                status, body = await _create(
                    console, http, name="   ", kind="client"
                )
                assert status == 400
                assert body["error"] == "invalid_token_name"

                # Aucun refus n'a écrit de Jeton.
                async with http.get(f"{console.url}{TOKENS_PATH}") as response:
                    assert await _json(response) == {"v": 1, "tokens": []}

                status, body = await _create(
                    console, http, name="Esp32", kind="source", source=" matrix "
                )
                assert status == 201
                assert body["token"]["kind"] == "source"
                assert body["token"]["source"] == "matrix"
        finally:
            await console.stop()

    asyncio.run(scenario())


def test_revocation_is_immediate_and_visible_in_the_list(tmp_path: Path) -> None:
    async def scenario() -> None:
        console = Console(tmp_path)
        await console.start()
        try:
            async with console.client() as http:
                await console.install(http)
                _, kept_body = await _create(
                    console, http, name="Portable", kind="client"
                )
                _, doomed_body = await _create(
                    console, http, name="Esp32", kind="source", source="esp"
                )
                kept = kept_body["token"]
                doomed = doomed_body["token"]

                console.clock.advance(30)
                async with http.delete(
                    f"{console.url}{TOKENS_PATH}/{doomed['id']}"
                ) as response:
                    assert response.status == 200
                    body = await _json(response)
                assert body["v"] == 1
                assert body["token"]["revoked_at"] == console.clock.value

                # La liste montre les deux, la révocation comprise.
                async with http.get(f"{console.url}{TOKENS_PATH}") as response:
                    listing = await _json(response)
                by_id = {token["id"]: token for token in listing["tokens"]}
                assert by_id[doomed["id"]]["revoked_at"] == console.clock.value
                assert by_id[kept["id"]]["revoked_at"] is None

                # La liste est la surface publique du cycle de vie : elle porte
                # la révocation immédiate et laisse l'autre Jeton actif. La
                # validation sur les surfaces machine est couverte par les
                # coutures Transport (#79) et Webhook (#80).

                # Révoquer à nouveau ne change pas la date.
                console.clock.advance(30)
                async with http.delete(
                    f"{console.url}{TOKENS_PATH}/{doomed['id']}"
                ) as response:
                    assert response.status == 200
                    assert (await _json(response))["token"]["revoked_at"] == (
                        console.clock.value - 30
                    )

                async with http.delete(
                    f"{console.url}{TOKENS_PATH}/inconnu"
                ) as response:
                    assert response.status == 404
                    assert (await _json(response))["error"] == "token_not_found"
        finally:
            await console.stop()

    asyncio.run(scenario())

