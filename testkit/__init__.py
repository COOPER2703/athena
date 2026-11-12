"""Harnais de test partagé : les cinq aides communes aux tests.

Le paquet expose **exactement cinq noms** — ``RecordingTraceSink``, ``FakeJudge``,
``wait_until``, ``url`` et ``register`` — et rien d'autre. Un test de plugin les importe
tels quels ; le ``conftest`` des tests du serveur les ré-exporte et garde le reste
(Console, horloge, aides de pilotage du Coordinateur).

Cette dépendance est assumée et consignée dans l'ADR-0023 : ``testkit`` importe
``server.core`` (``Judgment``), ``server.core.events`` et ``protocol`` ; le transport
WebSocket n'y entre que pour le typage (``TYPE_CHECKING``). Il n'importe jamais
``plugin_api``. Les tests de plugin, eux, traversent la vraie couture et importent
directement ``server.app``, ``server.adapters.*`` et ``server.core.*`` : c'est
l'arbitrage assumé — le paquet nomme les aides, il ne cache pas le serveur.
"""

import asyncio as _asyncio
from typing import TYPE_CHECKING as _TYPE_CHECKING

from protocol import (
    Register as _Register,
    Registered as _Registered,
    ToolDeclaration as _ToolDeclaration,
    decode as _decode,
    encode as _encode,
)
from server.core import Judgment as _Judgment
from server.core.events import ClientRegistered as _ClientRegistered

if _TYPE_CHECKING:
    from collections.abc import Callable
    from websockets.asyncio.client import ClientConnection
    from server.adapters.transport import WebSocketTransport
    from server.core import Judgment
    from server.core.ports import TraceEntry

__all__ = [
    "RecordingTraceSink",
    "FakeJudge",
    "wait_until",
    "url",
    "register",
]


class FakeJudge:
    """``EventJudge`` de test : verdict fixé, faits reçus conservés.

    Par défaut il annonce, pour que les tests du noyau qui atteignent l'échéance
    de fenêtre exercent le chemin d'Annonce sans dépendre d'un modèle. Les tests
    du filtre lui passent ``announce=False``.
    """

    def __init__(
        self,
        *,
        announce: bool = True,
        message: str = "Annonce",
        failed: bool = False,
    ) -> None:
        self._announce = announce
        self._message = message
        self._failed = failed
        self.calls: list[list[str]] = []

    async def judge(self, facts: list[str]) -> "Judgment":
        self.calls.append(list(facts))
        if self._failed:
            return _Judgment(announce=False, failed=True)
        if not self._announce:
            return _Judgment(announce=False)
        return _Judgment(announce=True, message=self._message)


class RecordingTraceSink:
    def __init__(self) -> None:
        self.entries: list[TraceEntry] = []

    def emit(self, entry: "TraceEntry") -> None:
        self.entries.append(entry)

    def of_kind(self, kind: str) -> list["TraceEntry"]:
        return [entry for entry in self.entries if entry.kind == kind]


async def wait_until(predicate: "Callable[[], bool]", timeout: float = 1.0) -> None:
    loop = _asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await _asyncio.sleep(0.001)


def url(transport: "WebSocketTransport") -> str:
    return f"ws://127.0.0.1:{transport.port}"


async def register(
    ws: "ClientConnection",
    transport: "WebSocketTransport",
    *,
    client_name: str = "desktop",
    platform: str = "linux",
    tools: "list[_ToolDeclaration] | None" = None,
    drain: bool = True,
) -> "_Registered":
    """Register a client and return the ``Registered`` acknowledgement.

    With ``drain`` the queued ``ClientRegistered`` event is consumed too (the
    adapter-owned registration path). Integration tests pass ``drain=False``
    because their core loop owns ``recv``.
    """
    await ws.send(
        _encode(
            _Register(
                client_name=client_name,
                platform=platform,
                client_tools=tools or [],
            )
        )
    )
    response = _decode(await _asyncio.wait_for(ws.recv(), 1.0))
    assert isinstance(response, _Registered)
    if drain:
        event = await _asyncio.wait_for(transport.recv(), 1.0)
        assert isinstance(event, _ClientRegistered)
        assert event.client_id == response.client_id
    return response
