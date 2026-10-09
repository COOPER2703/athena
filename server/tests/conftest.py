from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

from websockets.asyncio.client import ClientConnection

from protocol import Register, Registered, ToolDeclaration, decode, encode
from server.adapters.transport import WebSocketTransport
from server.core.events import ClientRegistered
from server.core.ports import TraceEntry


class RecordingTraceSink:
    def __init__(self) -> None:
        self.entries: list[TraceEntry] = []

    def emit(self, entry: TraceEntry) -> None:
        self.entries.append(entry)

    def of_kind(self, kind: str) -> list[TraceEntry]:
        return [entry for entry in self.entries if entry.kind == kind]


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


@asynccontextmanager
async def running_transport(
    *,
    sink: RecordingTraceSink | None = None,
    server_tools: list[str] | None = None,
) -> AsyncIterator[WebSocketTransport]:
    transport = WebSocketTransport(
        host="127.0.0.1",
        port=0,
        server_tools=server_tools,
        trace_sink=sink,
    )
    await transport.start()
    try:
        yield transport
    finally:
        await transport.stop()


def _url(transport: WebSocketTransport) -> str:
    return f"ws://127.0.0.1:{transport.port}"


async def register(
    ws: ClientConnection,
    transport: WebSocketTransport,
    *,
    client_name: str = "desktop",
    platform: str = "linux",
    tools: list[ToolDeclaration] | None = None,
    drain: bool = True,
) -> Registered:
    """Register a client and return the ``Registered`` acknowledgement.

    With ``drain`` the queued ``ClientRegistered`` event is consumed too (the
    adapter-owned registration path). Integration tests pass ``drain=False``
    because their core loop owns ``recv``.
    """
    await ws.send(
        encode(
            Register(
                client_name=client_name,
                platform=platform,
                client_tools=tools or [],
            )
        )
    )
    response = decode(await asyncio.wait_for(ws.recv(), 1.0))
    assert isinstance(response, Registered)
    if drain:
        event = await asyncio.wait_for(transport.recv(), 1.0)
        assert isinstance(event, ClientRegistered)
        assert event.client_id == response.client_id
    return response
