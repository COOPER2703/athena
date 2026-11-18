"""Conversation vocale de la Console (ticket #182, ADR-0027).

Deux seams sont exercés, sans jamais toucher aux internes du noyau :

- **l'adaptateur Console** : une vraie WebSocket ``/api/voice``, un vrai Ticket,
  une couture ``SessionControl`` factice — le contrat montant (binaire PCM) et
  descendant (JSON) est observé tel que le navigateur le voit ;
- **le serveur complet** : la racine de composition réelle, le faux modèle Live
  scriptable, l'instantané et le Journal — le Client Console naît, parle, reçoit
  l'audio et meurt avec sa Session, sans Client fantôme.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import websockets

from harness import Console
from protocol import Audio
from server.adapters.console.contract import VOICE_PATH
from server.adapters.console.voice import console_client_name
from server.core import (
    Coordinator,
    LlmOpened,
    SessionRequested,
    SessionState,
)
from server.core.commands import CloseLiveConnection, RefuseSession
from server.core.coordinator import IDLE_TIMEOUT
from server.core.events import TourRequested
from server.core.ports import SessionSurface
from server_harness import running_server

FIREFOX = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
)


# --- doubles de la couture ---------------------------------------------------


class FakeSessionControl:
    """Couture ``SessionControl`` : enregistre les demandes, refuse sur commande."""

    def __init__(self, refuse: str = "") -> None:
        self.refuse = refuse
        self.opened: list[tuple[str, str, SessionSurface]] = []
        self.audio: list[tuple[str, bytes]] = []
        self.closed: list[str] = []

    def open_session(
        self, *, client_id: str, client_name: str, surface: SessionSurface
    ) -> bool:
        if self.refuse:
            surface.refused(self.refuse)
            return False
        self.opened.append((client_id, client_name, surface))
        return True

    def send_audio(self, client_id: str, data: bytes) -> None:
        self.audio.append((client_id, data))

    def close_session(self, client_id: str) -> None:
        self.closed.append(client_id)


class _ConsoleHarness:
    """Adaptateur Console avec une vraie base d'auth et un port éphémère."""

    def __init__(self, tmp_path: Path, control: FakeSessionControl) -> None:
        self.console = Console(tmp_path)
        self.control = control

    @property
    def url(self) -> str:
        return self.console.url

    async def start(self) -> None:
        await self.console.start(self.control)

    async def stop(self) -> None:
        await self.console.stop()

    def client(self, *, cookies: bool = True) -> aiohttp.ClientSession:
        return self.console.client(cookies=cookies)

    async def install(self, http: aiohttp.ClientSession) -> None:
        await self.console.install(http)


# --- helpers -----------------------------------------------------------------


async def _next(ws: aiohttp.ClientWebSocketResponse, timeout: float = 2.0) -> dict:
    return await asyncio.wait_for(ws.receive_json(), timeout)


async def _next_type(
    ws: aiohttp.ClientWebSocketResponse,
    kind: str,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError(f"aucune trame {kind} en {timeout}s")
        frame = await asyncio.wait_for(ws.receive_json(), remaining)
        if frame.get("type") == kind:
            return frame


async def _open_voice(
    http: aiohttp.ClientSession,
    base_url: str,
    *,
    user_agent: str = FIREFOX,
) -> aiohttp.ClientWebSocketResponse:
    return await http.ws_connect(
        f"{base_url}{VOICE_PATH}",
        headers={"User-Agent": user_agent},
    )


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition jamais atteinte")
        await asyncio.sleep(0.01)


# --- nom du Client Console ---------------------------------------------------


def test_the_console_client_is_named_after_its_browser() -> None:
    """Le nom vient du seul ``User-Agent``, jamais de la page."""
    assert console_client_name(FIREFOX) == "Console (Firefox)"
    assert (
        console_client_name(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
        )
        == "Console (Safari)"
    )
    assert (
        console_client_name(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 "
            "Safari/537.36 Edg/126.0.0.0"
        )
        == "Console (Edge)"
    )
    assert console_client_name("") == "Console (Navigateur)"


# --- adaptateur : contrat de la WebSocket ------------------------------------


def test_the_voice_upgrade_is_refused_without_a_console_ticket(
    tmp_path: Path,
) -> None:
    """"Sans Ticket Console, aucun ``101`` : le refus précède toute trame."""

    async def scenario() -> None:
        harness = _ConsoleHarness(tmp_path, FakeSessionControl())
        await harness.start()
        try:
            async with harness.client() as http:
                await harness.install(http)

            # Autre navigateur, sans cookie.
            async with harness.client(cookies=False) as http:
                with pytest.raises(aiohttp.WSServerHandshakeError) as excinfo:
                    await _open_voice(http, harness.url)
                assert excinfo.value.status == 401

            assert harness.control.opened == []
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_upstream_audio_and_downstream_frames_use_the_documented_contract(
    tmp_path: Path,
) -> None:
    """Binaire montant → audio ; ``ready``/``transcript``/``audio``/``ended`` descendants."""

    async def scenario() -> None:
        control = FakeSessionControl()
        harness = _ConsoleHarness(tmp_path, control)
        await harness.start()
        try:
            async with harness.client() as http:
                await harness.install(http)
                async with await _open_voice(http, harness.url) as ws:
                    [(client_id, name, surface)] = control.opened
                    assert name == "Console (Firefox)"

                    surface.ready()
                    assert await _next(ws) == {
                        "v": 1,
                        "type": "ready",
                        "data": {},
                    }

                    await ws.send_bytes(b"\x01\x02")
                    await _wait_for(lambda: control.audio == [(client_id, b"\x01\x02")])

                    surface.transcript("user", "Bonjour Athena")
                    assert await _next(ws) == {
                        "v": 1,
                        "type": "transcript",
                        "data": {"role": "user", "text": "Bonjour Athena"},
                    }

                    surface.audio(b"\x03\x04")
                    audio = await _next(ws)
                    assert audio["type"] == "audio"
                    assert audio["data"]["mime"] == "audio/pcm;rate=24000"
                    assert base64.b64decode(audio["data"]["data"]) == b"\x03\x04"

                    surface.ended("COMPLETED")
                    assert await _next(ws) == {
                        "v": 1,
                        "type": "ended",
                        "data": {"reason": "COMPLETED"},
                    }
                    # La fin ferme la WebSocket : aucun Client fantôme.
                    message = await asyncio.wait_for(ws.receive(), 2.0)
                    assert message.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    )
                    await _wait_for(lambda: control.closed == [client_id])
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_a_refused_start_sends_a_stable_code_then_closes(tmp_path: Path) -> None:
    """Contention : un code stable et un message lisible, aucune trame d'audio."""

    async def scenario() -> None:
        control = FakeSessionControl(refuse="session_busy")
        harness = _ConsoleHarness(tmp_path, control)
        await harness.start()
        try:
            async with harness.client() as http:
                await harness.install(http)
                async with await _open_voice(http, harness.url) as ws:
                    frame = await _next(ws)
                    assert frame["v"] == 1
                    assert frame["type"] == "refused"
                    assert frame["data"]["code"] == "session_busy"
                    assert "conversation" in frame["data"]["message"]
                    message = await asyncio.wait_for(ws.receive(), 2.0)
                    assert message.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                    )

                assert control.closed == []
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_closing_the_socket_releases_the_console_client(tmp_path: Path) -> None:
    """Le navigateur qui ferme l'onglet libère le Client : ``close_session``."""

    async def scenario() -> None:
        control = FakeSessionControl()
        harness = _ConsoleHarness(tmp_path, control)
        await harness.start()
        try:
            async with harness.client() as http:
                await harness.install(http)
                ws = await _open_voice(http, harness.url)
                [(client_id, _, _)] = control.opened
                await ws.close()
                await _wait_for(lambda: control.closed == [client_id])
        finally:
            await harness.stop()

    asyncio.run(scenario())


# --- serveur complet ---------------------------------------------------------


def test_a_voice_conversation_runs_until_the_model_ends_it(tmp_path: Path) -> None:
    """Ouverture, audio montant, réponses descendantes, fin explicite COMPLETED."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            server.model.reply(
                "Bonjour Cooper.", audio=b"\x0a\x0b", heard="Salut Athena"
            )

            ws = await _open_voice(server.http, server.url)
            try:
                assert await _next_type(ws, "ready") == {
                    "v": 1,
                    "type": "ready",
                    "data": {},
                }

                await server.wait_until(
                    lambda: bool(server.app.snapshot()["clients"]), timeout=2.0
                )
                during = await server.status()
                console_client = during["clients"][0]
                assert console_client["client_name"] == "Console (Firefox)"
                assert console_client["platform"] == "web"
                assert during["session"]["console"] is True

                await ws.send_bytes(b"micro")

                frames = [await _next(ws) for _ in range(3)]
                transcripts = {
                    frame["data"]["role"]: frame["data"]["text"]
                    for frame in frames
                    if frame["type"] == "transcript"
                }
                assert transcripts == {
                    "user": "Salut Athena",
                    "assistant": "Bonjour Cooper.",
                }
                [audio] = [frame for frame in frames if frame["type"] == "audio"]
                assert audio["data"]["mime"] == "audio/pcm;rate=24000"
                assert base64.b64decode(audio["data"]["data"]) == b"\x0a\x0b"
                # Vos mots précèdent la réponse d'Athena, qui arrive d'un bloc.
                roles = [
                    frame["data"]["role"]
                    for frame in frames
                    if frame["type"] == "transcript"
                ]
                assert roles == ["user", "assistant"]

                visible = await server.status()
                assert visible["session"]["state"] == "SessionVisible"
                assert (
                    visible["session"]["active_client_id"]
                    == console_client["client_id"]
                )
                assert visible["session"]["console"] is True

                # Fin décidée par le modèle : ``end_session`` rend COMPLETED.
                # Le faux modèle ne livre son tour qu'à une entrée : on parle
                # encore une fois, comme un « au revoir ».
                server.model.call_tool("end_session", {})
                await ws.send_bytes(b"au revoir")
                ended = await _next_type(ws, "ended")
                assert ended["data"] == {"reason": "COMPLETED"}
            finally:
                await ws.close()

            await server.wait_until(
                lambda: server.app.snapshot()["clients"] == [], timeout=2.0
            )
            after = await server.status()
            assert after["clients"] == []
            assert after["session"]["console"] is False
            assert after["session"]["state"] == "Idle"

    asyncio.run(scenario())


def test_a_second_start_during_another_client_session_is_refused(
    tmp_path: Path,
) -> None:
    """Contention : refus stable, aucune préemption de la Session du desktop."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            server.model.reply("À tout de suite.", audio=b"reply")

            async with server.client(name="desktop") as client:
                await client.start_session()
                await client.send_audio(b"micro")
                await client.wait_message(Audio)

                ws = await _open_voice(server.http, server.url)
                try:
                    frame = await _next_type(ws, "refused")
                    assert frame["data"]["code"] == "session_busy"
                    assert frame["data"]["message"]
                finally:
                    await ws.close()

                during = await server.status()
                assert during["session"]["state"] == "SessionVisible"
                assert (
                    during["session"]["active_client_id"] == client.client_id
                )
                assert during["session"]["console"] is False
                assert [item["client_name"] for item in during["clients"]] == [
                    "desktop"
                ]

                # Le desktop n'a jamais été préempté : aucune trace de remplacement.
                assert not [
                    entry
                    for entry in server.trace_log()
                    if entry["kind"] == "SessionClosed"
                    and entry["payload"].get("reason") == "REPLACED"
                ]

    asyncio.run(scenario())


def test_inactivity_ends_the_console_conversation_with_timeout(
    tmp_path: Path,
) -> None:
    """Le timeout d'inactivité ordinaire s'applique, motif TIMEOUT."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            ws = await _open_voice(server.http, server.url)
            try:
                await _next_type(ws, "ready")
                server.clock.advance(IDLE_TIMEOUT + 1.0)
                ended = await _next_type(ws, "ended")
                assert ended["data"] == {"reason": "TIMEOUT"}
            finally:
                await ws.close()

            await server.wait_until(
                lambda: server.app.snapshot()["session"]["state"] == "Idle",
                timeout=2.0,
            )
            assert server.app.snapshot()["clients"] == []

    asyncio.run(scenario())


def test_closing_the_voice_socket_ends_the_session_with_client_gone(
    tmp_path: Path,
) -> None:
    """Une coupure de liaison termine la Session et ne laisse aucun Client."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            ws = await _open_voice(server.http, server.url)
            await _next_type(ws, "ready")
            await ws.close()

            await server.wait_until(
                lambda: server.app.snapshot()["session"]["state"] == "Idle",
                timeout=2.0,
            )
            assert server.app.snapshot()["clients"] == []
            await server.wait_until(
                lambda: any(
                    entry["kind"] == "SessionClosed"
                    for entry in server.trace_log()
                ),
                timeout=2.0,
            )
            closed = [
                entry
                for entry in server.trace_log()
                if entry["kind"] == "SessionClosed"
            ]
            assert closed[-1]["payload"]["reason"] == "CLIENT_GONE"

    asyncio.run(scenario())


def test_the_voice_route_does_not_weaken_the_transport_token(
    tmp_path: Path,
) -> None:
    """Le Ticket ouvre la voix ; le Protocole partagé garde son Jeton ``client``."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            with pytest.raises(websockets.exceptions.InvalidStatus) as excinfo:
                await websockets.connect(server.transport_url)
            assert excinfo.value.response.status_code == 401

    asyncio.run(scenario())


def test_a_voice_conversation_requires_the_installation_to_be_complete(
    tmp_path: Path,
) -> None:
    """Sans clé Gemini, aucun Client Console ne naît : refus lisible."""

    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as server:
            await server.install()
            ws = await _open_voice(server.http, server.url)
            try:
                frame = await _next_type(ws, "refused")
                assert frame["data"]["code"] == "installation_incomplete"
            finally:
                await ws.close()
            assert server.app.snapshot()["clients"] == []
            assert server.app.snapshot()["session"]["console"] is False

    asyncio.run(scenario())


# --- noyau : une demande non préemptante est refusée, jamais servie ----------


def test_a_non_preempting_request_never_takes_over_a_session() -> None:
    """La Console demande ; le noyau refuse au lieu de préempter (ADR-0027)."""

    async def scenario() -> None:
        coordinator = Coordinator()
        await coordinator.handle(SessionRequested(client_id="desktop"))
        await coordinator.handle(LlmOpened())

        commands = await coordinator.handle(
            SessionRequested(client_id="web", no_preempt=True)
        )

        assert RefuseSession(client_id="web", reason="session_busy") in commands
        assert not any(isinstance(command, CloseLiveConnection) for command in commands)
        assert coordinator.session_client_id == "desktop"
        assert coordinator.state is SessionState.LIVE_ACTIVE

    asyncio.run(scenario())


def test_a_non_preempting_request_is_refused_while_a_tour_holds_the_kernel() -> None:
    async def scenario() -> None:
        coordinator = Coordinator()
        await coordinator.handle(TourRequested(tour_id="tour", text="bonjour"))

        commands = await coordinator.handle(
            SessionRequested(client_id="web", no_preempt=True)
        )

        assert RefuseSession(client_id="web", reason="tour_busy") in commands
        assert coordinator.state is SessionState.LIVE_CONNECTING

    asyncio.run(scenario())
