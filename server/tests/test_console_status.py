"""Flux d'état de la Console : trames ``status`` différentielles.

Le serveur pousse l'instantané sur ``/api/stream`` à un tick de fond, et
seulement quand il a changé. Les tests parlent à l'adaptateur avec un vrai
client WebSocket ``aiohttp`` et un instantané injecté mutable.
"""

from __future__ import annotations

import asyncio

import aiohttp

from server.adapters.console import ConsoleAdapter
from server.adapters.console.trace_buffer import TraceBuffer
from server.core.ports import TraceEntry

STREAM_PATH = "/api/stream"
STATUS_PATH = "/api/status"
# Tick court pour que les tests restent rapides ; le défaut de production est
# d'environ une seconde.
TICK = 0.02


def _state() -> dict[str, object]:
    return {
        "session": {
            "state": "Idle",
            "session_id": "",
            "connection_id": "",
            "active_client_id": "",
        },
        "clients": [],
        "plugins": [],
        "config": {
            "model": "gemini-live",
            "voice": "Aoede",
            "user_id": "owner",
            "memory_db": "athena_memory.db",
            "log_dir": "logs",
            "version": "0.1.0",
        },
    }


def _ws_url(adapter: ConsoleAdapter) -> str:
    return f"ws://127.0.0.1:{adapter.port}{STREAM_PATH}"


def _status_url(adapter: ConsoleAdapter) -> str:
    return f"http://127.0.0.1:{adapter.port}{STATUS_PATH}"


async def _receive(ws: aiohttp.ClientWebSocketResponse) -> dict:
    return await asyncio.wait_for(ws.receive_json(), 1.0)


async def _expect_silence(
    ws: aiohttp.ClientWebSocketResponse, timeout: float = 0.1
) -> None:
    """Vérifie qu'aucune trame ne descend pendant au moins un tick."""
    try:
        message = await asyncio.wait_for(ws.receive(), timeout)
    except asyncio.TimeoutError:
        return
    raise AssertionError(f"trame inattendue : {message!r}")


def test_one_status_frame_per_change_reflects_the_new_state() -> None:
    """Un changement = une trame, avec exactement la forme de ``/api/status``."""

    async def scenario() -> None:
        state = _state()
        adapter = ConsoleAdapter(port=0, status_interval=TICK)
        await adapter.start(lambda: state)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as ws:
                    await _receive(ws)

                    session = state["session"]
                    assert isinstance(session, dict)
                    session["state"] = "SessionVisible"
                    session["session_id"] = "sess-1"
                    session["connection_id"] = "conn-1"
                    session["active_client_id"] = "client-1"

                    frame = await _receive(ws)
                    assert frame == {"v": 1, "type": "status", "data": state}

                    # Le corps est exactement celui de /api/status, enveloppe
                    # retirée : les deux surfaces ne peuvent pas diverger.
                    async with http.get(_status_url(adapter)) as response:
                        assert await response.json() == {"v": 1, **frame["data"]}

                    # Le tick suivant ne répète pas un état inchangé.
                    await _expect_silence(ws)

                    clients = state["clients"]
                    assert isinstance(clients, list)
                    clients.append(
                        {
                            "client_id": "client-1",
                            "client_name": "desktop",
                            "platform": "linux",
                        }
                    )
                    frame = await _receive(ws)
                    assert frame["data"]["clients"] == clients
                    await _expect_silence(ws)
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_key_order_does_not_count_as_a_change() -> None:
    """L'ordre d'insertion des clés ne déclenche pas de trame : égalité structurelle."""

    async def scenario() -> None:
        state = _state()

        def reordered() -> dict[str, object]:
            return {key: state[key] for key in reversed(list(state))}

        adapter = ConsoleAdapter(port=0, status_interval=TICK)
        await adapter.start(reordered)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as ws:
                    await _receive(ws)
                    await _expect_silence(ws)
                    await _expect_silence(ws)
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_a_change_before_connection_is_carried_by_hello_without_a_duplicate() -> None:
    """L'état du ``hello`` devient la référence : aucun ``status`` redondant.

    Le changement survient après la construction du diffuseur (qui a pris son
    empreinte de départ) mais avant l'abonnement du client. Le ``hello`` porte
    donc le nouvel état : il ne doit pas être suivi d'une trame ``status``
    identique au tick suivant.
    """

    async def scenario() -> None:
        state = _state()
        # Tick assez lent pour que le changement précède l'abonnement à coup
        # sûr, puis un tick effectif après le hello.
        slow_tick = 0.2
        adapter = ConsoleAdapter(port=0, status_interval=slow_tick)
        await adapter.start(lambda: state)
        try:
            session = state["session"]
            assert isinstance(session, dict)
            session["state"] = "SessionVisible"
            session["session_id"] = "sess-1"
            session["connection_id"] = "conn-1"
            session["active_client_id"] = "client-1"

            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as ws:
                    hello = await _receive(ws)
                    assert hello["type"] == "hello"
                    assert hello["data"]["snapshot"] == state

                    # Au moins un tick passe : l'état n'a pas changé depuis le
                    # hello, donc rien ne doit descendre.
                    await _expect_silence(ws, timeout=slow_tick * 3)
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_a_reconnecting_client_starts_from_the_current_state() -> None:
    """Une reconnexion reçoit l'instantané courant dans ``hello``, puis les deltas."""

    async def scenario() -> None:
        state = _state()
        adapter = ConsoleAdapter(port=0, status_interval=TICK)
        await adapter.start(lambda: state)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as first:
                    await _receive(first)
                    session = state["session"]
                    assert isinstance(session, dict)
                    session["state"] = "SessionVisible"
                    session["session_id"] = "sess-1"
                    await _receive(first)

                async with http.ws_connect(_ws_url(adapter)) as second:
                    hello = await _receive(second)
                    assert hello["type"] == "hello"
                    assert hello["data"]["snapshot"] == state

                    clients = state["clients"]
                    assert isinstance(clients, list)
                    clients.append(
                        {
                            "client_id": "client-1",
                            "client_name": "desktop",
                            "platform": "linux",
                        }
                    )
                    frame = await _receive(second)
                    assert frame["type"] == "status"
                    assert frame["data"]["clients"] == clients
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_status_and_trace_frames_share_the_stream() -> None:
    """Un changement d'état et une Trace vivante arrivent tous deux, sans se perdre."""

    async def scenario() -> None:
        state = _state()
        buffer = TraceBuffer(capacity=10)
        adapter = ConsoleAdapter(
            port=0, trace_buffer=buffer, status_interval=TICK
        )
        await adapter.start(lambda: state)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as ws:
                    await _receive(ws)

                    state["clients"] = [
                        {
                            "client_id": "client-1",
                            "client_name": "desktop",
                            "platform": "linux",
                        }
                    ]
                    buffer.emit(
                        TraceEntry(
                            timestamp=1.0,
                            source="core",
                            kind="Live",
                            payload={},
                        )
                    )

                    seen: dict[str, dict] = {}
                    for _ in range(2):
                        frame = await _receive(ws)
                        seen[frame["type"]] = frame

                    assert seen["status"]["data"]["clients"] == state["clients"]
                    assert seen["trace"]["data"]["kind"] == "Live"
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_the_status_tick_stops_with_the_adapter() -> None:
    """Après ``stop``, l'instantané n'est plus interrogé : aucune tâche orpheline."""

    async def scenario() -> None:
        state = _state()
        calls = 0

        def snapshot() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return state

        adapter = ConsoleAdapter(port=0, status_interval=TICK)
        await adapter.start(snapshot)
        async with aiohttp.ClientSession() as http:
            ws = await http.ws_connect(_ws_url(adapter))
            await _receive(ws)

            await asyncio.sleep(0.1)
            assert calls >= 2

            await adapter.stop()
            after_stop = calls
            await asyncio.sleep(0.1)
            assert calls == after_stop

    asyncio.run(scenario())


def test_no_status_frame_is_pushed_when_the_snapshot_does_not_change() -> None:
    """Un instantané stable ne produit aucun trafic, tick après tick."""

    async def scenario() -> None:
        state = _state()
        adapter = ConsoleAdapter(port=0, status_interval=TICK)
        await adapter.start(lambda: state)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as ws:
                    hello = await _receive(ws)
                    assert hello["type"] == "hello"
                    assert hello["data"]["snapshot"] == state

                    await _expect_silence(ws)
                    await _expect_silence(ws)
        finally:
            await adapter.stop()

    asyncio.run(scenario())
