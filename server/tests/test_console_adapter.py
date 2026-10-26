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
