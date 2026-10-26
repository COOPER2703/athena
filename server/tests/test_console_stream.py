"""Flux de traces de la Console web : WebSocket ``GET /api/stream``.

Les tests parlent à l'adaptateur avec un vrai client WebSocket ``aiohttp`` et
un vrai tampon borné. Le passage du backlog au direct et la troncature par
client sont vérifiés sans mock du transport.
"""

from __future__ import annotations

import asyncio
import io
import logging
import socket
from typing import Mapping

import aiohttp

from conftest import wait_until
from server.adapters.console import ConsoleAdapter
from server.adapters.console.stream import trace_frame
from server.adapters.console.trace_buffer import (
    TRUNCATION_KIND,
    TraceBuffer,
    truncation_entry,
)
from server.app.config import (
    AppConfig,
    ConsoleConfig,
    GeminiConfig,
    PluginsConfig,
    ServerConfig,
)
from server.app.root import build_app
from server.app.trace import build_trace_sink
from server.core.ports import TraceEntry

STREAM_PATH = "/api/stream"


def _snapshot() -> dict[str, object]:
    return {
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


def _entry(
    kind: str,
    *,
    timestamp: float = 1_700_000_000.0,
    level: str = "info",
    source: str = "core",
    payload: Mapping[str, object] | None = None,
) -> TraceEntry:
    return TraceEntry(
        timestamp=timestamp,
        source=source,
        kind=kind,
        payload=payload or {},
        level=level,
    )


def _trace(kind: str, **kwargs: object) -> dict[str, object]:
    entry = _entry(kind, **kwargs)
    return {
        "timestamp": entry.timestamp,
        "source": entry.source,
        "kind": entry.kind,
        "level": entry.level,
        "payload": dict(entry.payload),
    }


def _ws_url(adapter: ConsoleAdapter) -> str:
    return f"ws://127.0.0.1:{adapter.port}{STREAM_PATH}"


async def _receive(ws: aiohttp.ClientWebSocketResponse) -> dict:
    return await asyncio.wait_for(ws.receive_json(), 1.0)


def test_hello_carries_the_snapshot_and_the_whole_backlog() -> None:
    """Une seule trame ``hello`` : instantané et backlog, tous niveaux compris."""

    async def scenario() -> None:
        buffer = TraceBuffer(capacity=10)
        buffer.emit(_entry("Before", timestamp=10.0))
        buffer.emit(_entry("DebugBefore", timestamp=11.0, level="debug"))
        adapter = ConsoleAdapter(port=0, trace_buffer=buffer)
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as ws:
                    hello = await _receive(ws)
                    assert hello == {
                        "v": 1,
                        "type": "hello",
                        "data": {
                            "snapshot": _snapshot(),
                            "backlog": [
                                _trace("Before", timestamp=10.0),
                                _trace("DebugBefore", timestamp=11.0, level="debug"),
                            ],
                        },
                    }
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_live_traces_follow_the_hello() -> None:
    """Après ``hello``, chaque entrée du flux arrive en trame ``trace``."""

    async def scenario() -> None:
        buffer = TraceBuffer(capacity=10)
        adapter = ConsoleAdapter(port=0, trace_buffer=buffer)
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as ws:
                    hello = await _receive(ws)
                    assert hello["data"]["backlog"] == []

                    buffer.emit(
                        _entry(
                            "LlmOpened",
                            timestamp=12.5,
                            source="gemini",
                            payload={"connection_id": "c1"},
                        )
                    )

                    frame = await _receive(ws)
                    assert frame == {
                        "v": 1,
                        "type": "trace",
                        "data": _trace(
                            "LlmOpened",
                            timestamp=12.5,
                            source="gemini",
                            payload={"connection_id": "c1"},
                        ),
                    }
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_backlog_to_live_is_atomic_without_gap_or_duplicate() -> None:
    """Émis avant la connexion → backlog ; émis après → direct, sans recouvrement."""

    async def scenario() -> None:
        buffer = TraceBuffer(capacity=10)
        buffer.emit(_entry("BeforeA", timestamp=1.0))
        buffer.emit(_entry("BeforeB", timestamp=2.0))
        adapter = ConsoleAdapter(port=0, trace_buffer=buffer)
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as ws:
                    hello = await _receive(ws)
                    assert [item["kind"] for item in hello["data"]["backlog"]] == [
                        "BeforeA",
                        "BeforeB",
                    ]

                    buffer.emit(_entry("LiveC", timestamp=3.0))
                    buffer.emit(_entry("LiveD", timestamp=4.0))

                    frames = [await _receive(ws), await _receive(ws)]
                    assert [frame["data"]["kind"] for frame in frames] == [
                        "LiveC",
                        "LiveD",
                    ]
                    assert [frame["data"]["timestamp"] for frame in frames] == [
                        3.0,
                        4.0,
                    ]
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_slow_client_loses_the_oldest_and_gets_a_single_truncation_marker() -> None:
    """File pleine : les plus anciennes partent, un seul marqueur les signale.

    Le marqueur est une Trace de ``kind`` réservé ``TraceTruncated`` ; son
    payload indique le nombre d'entrées perdues lors de la première troncature.
    """

    async def scenario() -> None:
        buffer = TraceBuffer(capacity=2)
        subscription = buffer.subscribe()
        for index in range(5):
            buffer.emit(_entry(f"E{index}", timestamp=float(index)))

        marker = await asyncio.wait_for(subscription.__anext__(), 1.0)
        assert marker.kind == TRUNCATION_KIND
        assert marker.source == "console"
        assert marker.level == "warning"
        assert marker.payload == {"dropped": 3}

        assert (await asyncio.wait_for(subscription.__anext__(), 1.0)).kind == "E3"
        assert (await asyncio.wait_for(subscription.__anext__(), 1.0)).kind == "E4"

        # Une seconde perte ne répète pas le marqueur : le client sait déjà que
        # son flux est incomplet.
        for index in range(5, 8):
            buffer.emit(_entry(f"E{index}", timestamp=float(index)))
        assert (await asyncio.wait_for(subscription.__anext__(), 1.0)).kind == "E6"
        buffer.unsubscribe(subscription)

    asyncio.run(scenario())


def test_buffer_keeps_every_level_and_bounds_its_memory() -> None:
    """Le tampon conserve tous les niveaux et oublie les plus anciennes entrées."""

    async def scenario() -> None:
        buffer = TraceBuffer(capacity=2)
        buffer.emit(_entry("Debug", level="debug"))
        buffer.emit(_entry("Info", level="info"))
        buffer.emit(_entry("Warning", level="warning"))

        subscription = buffer.subscribe()
        assert [entry.kind for entry in subscription.backlog] == ["Info", "Warning"]
        buffer.unsubscribe(subscription)

    asyncio.run(scenario())


def test_two_clients_each_receive_the_live_stream() -> None:
    """Chaque client a sa propre file : les deux reçoivent les mêmes trames."""

    async def scenario() -> None:
        buffer = TraceBuffer(capacity=10)
        adapter = ConsoleAdapter(port=0, trace_buffer=buffer)
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.ws_connect(_ws_url(adapter)) as first:
                    await _receive(first)
                    async with http.ws_connect(_ws_url(adapter)) as second:
                        await _receive(second)
                        assert buffer.subscriber_count == 2

                        buffer.emit(_entry("Shared", timestamp=9.0))
                        assert (await _receive(first))["data"]["kind"] == "Shared"
                        assert (await _receive(second))["data"]["kind"] == "Shared"
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_disconnect_removes_the_subscription() -> None:
    """Fermer le socket retire l'abonné : aucune file ne fuit."""

    async def scenario() -> None:
        buffer = TraceBuffer(capacity=10)
        adapter = ConsoleAdapter(port=0, trace_buffer=buffer)
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as http:
                ws = await http.ws_connect(_ws_url(adapter))
                await _receive(ws)
                assert buffer.subscriber_count == 1

                await ws.close()
                await wait_until(lambda: buffer.subscriber_count == 0)
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_stop_closes_open_streams_and_removes_subscriptions() -> None:
    """``stop`` termine les abonnés et ferme les sockets ouvertes."""

    async def scenario() -> None:
        buffer = TraceBuffer(capacity=10)
        adapter = ConsoleAdapter(port=0, trace_buffer=buffer)
        await adapter.start(_snapshot)
        async with aiohttp.ClientSession() as http:
            ws = await http.ws_connect(_ws_url(adapter))
            await _receive(ws)

            await adapter.stop()

            try:
                message = await asyncio.wait_for(ws.receive(), 1.0)
            except aiohttp.ServerDisconnectedError:
                pass
            else:
                assert message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                }
            assert buffer.subscriber_count == 0

    asyncio.run(scenario())


def test_stream_requires_a_websocket_upgrade() -> None:
    """Un GET HTTP ordinaire sur ``/api/stream`` reste une erreur JSON."""

    async def scenario() -> None:
        adapter = ConsoleAdapter(port=0)
        await adapter.start(_snapshot)
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"http://127.0.0.1:{adapter.port}{STREAM_PATH}"
                ) as response:
                    assert response.status == 400
                    assert response.content_type == "application/json"
                    assert (await response.json())["error"]
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_the_fan_out_feeds_the_console_buffer_with_every_level() -> None:
    """Un sink supplémentaire reçoit tout le flux unique, sans filtre de niveau."""

    buffer = TraceBuffer(capacity=10)
    sink = build_trace_sink(stream=io.StringIO(), extra_sinks=[buffer])
    sink.emit(_entry("Info", level="info"))
    sink.emit(_entry("Debug", level="debug"))

    subscription = buffer.subscribe()
    assert [entry.kind for entry in subscription.backlog] == ["Info", "Debug"]
    buffer.unsubscribe(subscription)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FakeMemory:
    """Mémoire factice : ``build_app`` ne charge ni modèle ni base réelle."""


async def _connect_stream_when_ready(
    http: aiohttp.ClientSession, url: str
) -> aiohttp.ClientWebSocketResponse:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while True:
        try:
            return await http.ws_connect(url)
        except aiohttp.ClientConnectorError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.01)


async def _assert_started_and_live(
    ws: aiohttp.ClientWebSocketResponse,
) -> None:
    hello = await asyncio.wait_for(ws.receive_json(), 1.0)
    assert hello["type"] == "hello"
    assert "started" in [entry["kind"] for entry in hello["data"]["backlog"]]

    logging.getLogger("fixture").warning("live")

    live = None
    while live is None:
        frame = await asyncio.wait_for(ws.receive_json(), 1.0)
        if frame["data"]["kind"] == "fixture":
            live = frame
    assert live["type"] == "trace"
    assert live["data"]["source"] == "logging"
    assert live["data"]["level"] == "warning"
    assert live["data"]["payload"] == {"message": "live"}


def test_build_app_feeds_the_stream_from_the_single_trace_flow(
    tmp_path, monkeypatch
) -> None:
    """Câblage réel : le flux ``/api/stream`` reçoit le fan-out de l'App."""

    async def scenario() -> None:
        monkeypatch.setattr(
            "server.app.root.SentenceTransformerEmbedder", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "server.app.root.SqliteMemoryStore", lambda *a, **k: _FakeMemory()
        )
        console_port = _free_port()
        config = AppConfig(
            gemini=GeminiConfig(api_key="test-key"),
            server=ServerConfig(host="127.0.0.1", port=_free_port()),
            console=ConsoleConfig(port=console_port),
            plugins=PluginsConfig(
                directory=str(tmp_path / "plugins"),
                state_path=str(tmp_path / "plugins.json"),
            ),
            log_dir=str(tmp_path),
        )
        root_logger = logging.getLogger()
        original_handlers = root_logger.handlers[:]
        original_level = root_logger.level
        app = build_app(config, secrets=None, env={}, env_file=None)
        runner = asyncio.create_task(app.run())
        try:
            async with aiohttp.ClientSession() as http:
                ws = await _connect_stream_when_ready(
                    http, f"ws://127.0.0.1:{console_port}{STREAM_PATH}"
                )
                await _assert_started_and_live(ws)
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 1.0)
            root_logger.handlers[:] = original_handlers
            root_logger.setLevel(original_level)

    asyncio.run(scenario())


def test_truncation_marker_serialises_as_a_reserved_trace_frame() -> None:
    """Le marqueur garde la forme d'une trame ``trace`` : aucun type exotique."""

    frame = trace_frame(truncation_entry(3))

    assert frame["v"] == 1
    assert frame["type"] == "trace"
    assert frame["data"]["source"] == "console"
    assert frame["data"]["kind"] == TRUNCATION_KIND
    assert frame["data"]["level"] == "warning"
    assert frame["data"]["payload"] == {"dropped": 3}
