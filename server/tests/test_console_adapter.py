from __future__ import annotations

import asyncio

import aiohttp
import pytest

from server.adapters.console import STATUS_PATH, ConsoleAdapter


def _url(adapter: ConsoleAdapter, path: str = STATUS_PATH) -> str:
    return f"http://127.0.0.1:{adapter.port}{path}"


def _snapshot() -> dict[str, object]:
    return {
        "session": {
            "state": "SessionVisible",
            "session_id": "sess-1",
            "connection_id": "s1",
            "active_client_id": "client-1",
        },
        "clients": [
            {"client_id": "client-1", "client_name": "desktop", "platform": "linux"},
            {"client_id": "client-2", "client_name": "phone", "platform": "android"},
        ],
        "plugins": [
            {"name": "homeassistant", "state": "activated", "tools": ["ha__turn_on"]}
        ],
        "config": {
            "model": "gemini-live",
            "voice": "Aoede",
            "user_id": "owner",
            "memory_db": "athena_memory.db",
            "log_dir": "logs",
            "version": "0.1.0",
        },
    }


def test_status_returns_the_versioned_snapshot() -> None:
    async def scenario() -> None:
        adapter = ConsoleAdapter(port=0)
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_url(adapter)) as response:
                    assert response.status == 200
                    assert await response.json() == {"v": 1, **_snapshot()}
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_stopping_the_console_stops_serving() -> None:
    async def scenario() -> None:
        adapter = ConsoleAdapter(port=0)
        await adapter.start(_snapshot)
        port = adapter.port
        await adapter.stop()

        async with aiohttp.ClientSession() as session:
            try:
                await session.get(f"http://127.0.0.1:{port}{STATUS_PATH}")
            except aiohttp.ClientConnectorError:
                pass
            else:  # pragma: no cover - the surface must not answer
                raise AssertionError("a stopped console still serves requests")

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/api", "/api/", "/api/unknown"])
def test_unknown_api_path_is_a_json_not_found(path: str) -> None:
    async def scenario() -> None:
        adapter = ConsoleAdapter(port=0)
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_url(adapter, path)) as response:
                    assert response.status == 404
                    assert response.content_type == "application/json"
                    body = await response.json()
                    assert body["error"]
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_each_request_reflects_the_current_snapshot() -> None:
    async def scenario() -> None:
        state = {
            "session": {
                "state": "Idle",
                "session_id": "",
                "connection_id": "",
                "active_client_id": "",
            },
            "clients": [],
            "plugins": [],
            "config": {},
        }
        adapter = ConsoleAdapter(port=0)
        await adapter.start(lambda: state)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_url(adapter)) as response:
                    first = await response.json()
                assert first["session"]["state"] == "Idle"
                assert first["clients"] == []

                state["session"] = {
                    "state": "SessionVisible",
                    "session_id": "sess-1",
                    "connection_id": "s1",
                    "active_client_id": "client-1",
                }
                state["clients"] = [
                    {
                        "client_id": "client-1",
                        "client_name": "desktop",
                        "platform": "linux",
                    }
                ]

                async with session.get(_url(adapter)) as response:
                    second = await response.json()
                assert second["session"]["state"] == "SessionVisible"
                assert second["session"]["active_client_id"] == "client-1"
                assert second["clients"] == [
                    {
                        "client_id": "client-1",
                        "client_name": "desktop",
                        "platform": "linux",
                    }
                ]
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_without_a_build_the_root_explains_what_to_run(tmp_path) -> None:
    """Sans build, `/` explique quoi lancer et l'API continue de répondre."""

    async def scenario() -> None:
        adapter = ConsoleAdapter(port=0, static_dir=tmp_path / "absent")
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_url(adapter, "/")) as response:
                    assert response.status == 200
                    assert response.content_type == "text/html"
                    page = await response.text()
                assert "make web" in page
                assert "npm run build" in page

                async with session.get(_url(adapter)) as response:
                    assert response.status == 200
                    assert (await response.json())["v"] == 1
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def _write_build(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    index = dist / "index.html"
    index.write_text("<!doctype html><div id=\"root\"></div>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('athena');", encoding="utf-8")
    return dist


def test_build_is_served_with_spa_fallback(tmp_path) -> None:
    """Le build présent sert `/`, les fichiers statiques et les chemins inconnus."""

    async def scenario() -> None:
        adapter = ConsoleAdapter(port=0, static_dir=_write_build(tmp_path))
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_url(adapter, "/")) as response:
                    assert response.status == 200
                    assert response.content_type == "text/html"
                    assert 'id="root"' in await response.text()

                async with session.get(_url(adapter, "/clients")) as response:
                    assert response.status == 200
                    assert 'id="root"' in await response.text()

                async with session.get(_url(adapter, "/assets/app.js")) as response:
                    assert response.status == 200
                    body = await response.text()
                assert body == "console.log('athena');"

                async with session.get(_url(adapter, "/api/inconnu")) as response:
                    assert response.status == 404
                    assert response.content_type == "application/json"
        finally:
            await adapter.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/%2e%2e/%2e%2e/secret.txt"])
def test_a_static_path_cannot_escape_the_build_directory(tmp_path, path) -> None:
    """Aucun chemin ne sert un fichier hors du répertoire statique."""

    async def scenario() -> None:
        secret = tmp_path / "secret.txt"
        secret.write_text("do not leak", encoding="utf-8")
        adapter = ConsoleAdapter(port=0, static_dir=_write_build(tmp_path))
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_url(adapter, path)) as response:
                    body = await response.text()
                    assert "do not leak" not in body
                    assert response.status in {200, 404}
        finally:
            await adapter.stop()

    asyncio.run(scenario())
