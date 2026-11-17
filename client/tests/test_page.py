from __future__ import annotations

import asyncio
from typing import Any, Mapping

import aiohttp

from client.page import CouplingPage, is_websocket_url


BASE_STATE: dict[str, object] = {
    "server_url": "ws://localhost:8765",
    "server_url_source": "file",
    "token_configured": True,
    "token_source": "file",
    "connection": "connected",
    "connection_message": "Connecté au serveur.",
    "mic": "ready",
    "mic_message": "Micro ouvert.",
    "wake_word": "ready",
    "wake_word_message": "Veille active.",
    "client_state": "listening",
    "client_id": "client-1",
}


class FakeController:
    def __init__(self) -> None:
        self.state: dict[str, object] = dict(BASE_STATE)
        # Un contrôleur réel ne rend jamais le Jeton ; ce faux le fait pour
        # prouver que la page le filtre de toute façon.
        self.state["access_token"] = "ath_secret"
        self.applied: list[tuple[str, str | None]] = []
        self.reconnects = 0

    def coupling_state(self) -> Mapping[str, object]:
        return dict(self.state)

    async def apply_coupling(
        self, *, server_url: str, access_token: str | None
    ) -> Mapping[str, object]:
        self.applied.append((server_url, access_token))
        self.state["server_url"] = server_url
        return self.coupling_state()

    async def reconnect(self) -> Mapping[str, object]:
        self.reconnects += 1
        return self.coupling_state()


async def with_page(controller: FakeController, scenario: Any) -> None:
    page = CouplingPage(controller, port=0)
    await page.start()
    try:
        base = f"http://127.0.0.1:{page.port}"
        async with aiohttp.ClientSession() as session:
            await scenario(session, base)
    finally:
        await page.stop()


def test_the_page_offers_the_coupling_controls() -> None:
    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/") as response:
            assert response.status == 200
            body = await response.text()
        assert "URL du serveur" in body
        assert "Jeton d'accès" in body
        assert "Micro" in body
        assert "Wake word" in body
        assert "Reconnecter" in body
        assert "/api/state" in body
        assert "ath_secret" not in body

    asyncio.run(with_page(FakeController(), scenario))


def test_the_state_route_hides_any_token() -> None:
    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/api/state") as response:
            assert response.status == 200
            state = await response.json()
        assert state["server_url"] == "ws://localhost:8765"
        assert state["token_configured"] is True
        assert state["connection"] == "connected"
        assert "access_token" not in state
        assert "ath_secret" not in str(state)

    asyncio.run(with_page(FakeController(), scenario))


def test_a_foreign_host_is_refused() -> None:
    controller = FakeController()

    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.get(f"{base}/", headers={"Host": "evil.example"}) as r:
            assert r.status == 403
        async with session.get(
            f"{base}/api/state", headers={"Host": "evil.example"}
        ) as r:
            assert r.status == 403
            assert (await r.json())["error"] == "forbidden"

    asyncio.run(with_page(controller, scenario))
    assert controller.applied == []


def test_a_foreign_origin_is_refused_before_the_controller() -> None:
    controller = FakeController()

    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(
            f"{base}/api/coupling",
            json={"server_url": "ws://evil:1", "access_token": "ath_x"},
            headers={"Origin": "http://evil.example"},
        ) as response:
            assert response.status == 403
            assert (await response.json())["error"] == "forbidden"

    asyncio.run(with_page(controller, scenario))
    assert controller.applied == []


def test_a_local_origin_is_accepted() -> None:
    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(
            f"{base}/api/coupling",
            json={"server_url": "ws://localhost:9000", "access_token": "ath_new"},
            headers={"Origin": base},
        ) as response:
            assert response.status == 200
            state = await response.json()
        assert state["server_url"] == "ws://localhost:9000"

    asyncio.run(with_page(FakeController(), scenario))


def test_saving_the_coupling_reaches_the_controller() -> None:
    controller = FakeController()

    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(
            f"{base}/api/coupling",
            json={
                "server_url": "ws://athena.example:8765",
                "access_token": "ath_secret",
            },
        ) as response:
            assert response.status == 200
            body = await response.text()
        assert "ath_secret" not in body

    asyncio.run(with_page(controller, scenario))
    assert controller.applied == [("ws://athena.example:8765", "ath_secret")]


def test_an_empty_token_keeps_the_stored_one() -> None:
    controller = FakeController()

    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(
            f"{base}/api/coupling",
            json={"server_url": "ws://athena.example:8765", "access_token": ""},
        ) as response:
            assert response.status == 200

    asyncio.run(with_page(controller, scenario))
    assert controller.applied == [("ws://athena.example:8765", None)]


def test_an_invalid_url_is_refused() -> None:
    controller = FakeController()

    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(
            f"{base}/api/coupling", json={"server_url": "http://example"}
        ) as response:
            assert response.status == 400
            assert (await response.json())["error"] == "invalid_url"
        async with session.post(
            f"{base}/api/coupling", json={"server_url": ""}
        ) as response:
            assert response.status == 400

    asyncio.run(with_page(controller, scenario))
    assert controller.applied == []


def test_the_reconnect_button_reaches_the_controller() -> None:
    controller = FakeController()

    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(f"{base}/api/reconnect") as response:
            assert response.status == 200

    asyncio.run(with_page(controller, scenario))
    assert controller.reconnects == 1


def test_a_store_failure_is_explained() -> None:
    class FailingController(FakeController):
        async def apply_coupling(
            self, *, server_url: str, access_token: str | None
        ) -> Mapping[str, object]:
            raise OSError("read-only file system")

    async def scenario(session: aiohttp.ClientSession, base: str) -> None:
        async with session.post(
            f"{base}/api/coupling", json={"server_url": "ws://localhost:8765"}
        ) as response:
            assert response.status == 500
            payload = await response.json()
        assert payload["error"] == "store_error"
        assert "read-only" not in payload["message"]

    asyncio.run(with_page(FailingController(), scenario))


def test_websocket_urls_are_recognised() -> None:
    assert is_websocket_url("ws://localhost:8765")
    assert is_websocket_url("wss://athena.example/ws")
    assert not is_websocket_url("http://localhost:8765")
    assert not is_websocket_url("ws://")
    assert not is_websocket_url("")
