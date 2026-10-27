from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable, Mapping

import aiohttp
from websockets.asyncio.client import ClientConnection

from protocol import Register, Registered, ToolDeclaration, decode, encode
from server.adapters.auth import SqliteAuthStore
from server.adapters.console import ConsoleAdapter
from server.adapters.console.contract import SETUP_PATH
from server.adapters.transport import WebSocketTransport
from server.core import (
    EVENT_WINDOW_TIMER_ID,
    Coordinator,
    Judgment,
    LlmAudio,
    LlmOpened,
    OpenLiveConnection,
    SendAnnouncementToLlm,
    SessionRequested,
    TimerFired,
    Trace,
)
from server.core.events import ClientRegistered
from server.core.ports import AuthService, TraceEntry


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

    async def judge(self, facts: list[str]) -> Judgment:
        self.calls.append(list(facts))
        if self._failed:
            return Judgment(announce=False, failed=True)
        if not self._announce:
            return Judgment(announce=False)
        return Judgment(announce=True, message=self._message)

class RecordingTraceSink:
    def __init__(self) -> None:
        self.entries: list[TraceEntry] = []

    def emit(self, entry: TraceEntry) -> None:
        self.entries.append(entry)

    def of_kind(self, kind: str) -> list[TraceEntry]:
        return [entry for entry in self.entries if entry.kind == kind]


TEST_PASSWORD = "correct horse battery staple"


class FakeClock:
    """Horloge réglable, partagée par les tests qui traversent les seuils d'auth."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        self.value = now

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def console_snapshot() -> Mapping[str, object]:
    """Instantané minimal d'une Console avec une Session visible."""
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
    """Console démarrée sur un port éphémère avec une vraie base d'auth.

    Le client HTTP se comporte comme un navigateur : il conserve le cookie
    ``Secure`` et envoie l'``Origin`` de la page sur chaque requête. Les tests
    qui visent précisément l'absence d'``Origin`` créent un client
    ``client(origin=False)``.
    """

    def __init__(
        self,
        tmp_path: Path,
        *,
        clock: FakeClock | None = None,
        snapshot: Callable[[], Mapping[str, object]] = console_snapshot,
        static_dir: Path | None = None,
        status_interval: float = 1.0,
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
            status_interval=status_interval,
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

    def client(
        self, *, cookies: bool = True, origin: bool = True
    ) -> aiohttp.ClientSession:
        jar = (
            aiohttp.CookieJar(unsafe=True, treat_as_secure_origin=self.url)
            if cookies
            else aiohttp.DummyCookieJar()
        )
        headers = {"Origin": self.url} if origin else {}
        return aiohttp.ClientSession(cookie_jar=jar, headers=headers)

    def install_code(self) -> str:
        code = self.store.announce_installation_code()
        assert code is not None
        return code

    async def install(
        self,
        http: aiohttp.ClientSession,
        *,
        username: str = "admin",
        password: str = TEST_PASSWORD,
    ) -> None:
        """Installe le Compte par la vraie route ; le jar garde le Ticket."""
        async with http.post(
            f"{self.url}{SETUP_PATH}",
            json={
                "code": self.install_code(),
                "username": username,
                "password": password,
            },
        ) as response:
            assert response.status == 200, await response.text()


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.001)


_INSTALLATION_CODE = re.compile(r'"code": "([^"]+)"')


async def read_installation_code(path: Path, timeout: float = 5.0) -> str:
    """Lit le Code d'installation écrit au démarrage dans le journal."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if path.is_file():
            match = _INSTALLATION_CODE.search(path.read_text(encoding="utf-8"))
            if match is not None:
                return match.group(1)
        if loop.time() > deadline:
            raise AssertionError(f"aucun Code d'installation dans {path}")
        await asyncio.sleep(0.01)


def console_session(base_url: str) -> aiohttp.ClientSession:
    """Client HTTP de Console : cookie ``Secure`` accepté, ``Origin`` de page.

    Il se comporte comme le navigateur de la Console : les requêtes mutantes
    portent l'origine de l'hôte servi, exigée par le contrôle CSRF (ADR-0017).
    """
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(
            unsafe=True, treat_as_secure_origin=base_url
        ),
        headers={"Origin": base_url},
    )


async def install_console(
    http: aiohttp.ClientSession,
    base_url: str,
    log_path: Path,
    *,
    username: str = "admin",
    password: str = TEST_PASSWORD,
    timeout: float = 5.0,
) -> None:
    """Installe l'instance à partir du journal et garde le Ticket dans le jar."""
    code = await read_installation_code(log_path, timeout)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            async with http.post(
                f"{base_url}/api/setup",
                json={"code": code, "username": username, "password": password},
            ) as response:
                assert response.status == 200, await response.text()
                return
        except aiohttp.ClientConnectorError:
            if loop.time() > deadline:
                raise
            await asyncio.sleep(0.01)


@asynccontextmanager
async def running_transport(
    *,
    sink: RecordingTraceSink | None = None,
    server_tools: list[str] | Callable[[], Iterable[str]] | None = None,
    auth: AuthService | None = None,
) -> AsyncIterator[WebSocketTransport]:
    transport = WebSocketTransport(
        host="127.0.0.1",
        port=0,
        server_tools=server_tools,
        trace_sink=sink,
        auth=auth,
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


# ---------------------------------------------------------------------------
# Aides partagées de la couture ``Coordinator.handle`` : une liste d'Événements
# en entrée, une liste de Commandes en sortie. Elles ne portent que sur le
# comportement externe, jamais sur l'état interne du noyau.
# ---------------------------------------------------------------------------


def handle(coordinator: Coordinator, event) -> list:
    return asyncio.run(coordinator.handle(event))


def drive(coordinator: Coordinator, events) -> list:
    commands: list = []
    for event in events:
        commands.extend(handle(coordinator, event))
    return commands


def effects(commands) -> list:
    return [command for command in commands if not isinstance(command, Trace)]


def traces(commands) -> list[Trace]:
    return [command for command in commands if isinstance(command, Trace)]


def window_timer() -> TimerFired:
    """L'échéance de la fenêtre, telle que l'horloge la livre."""
    return TimerFired(connection_id=EVENT_WINDOW_TIMER_ID)


def close_window(coordinator: Coordinator) -> list:
    return handle(coordinator, window_timer())


def opened(commands) -> OpenLiveConnection:
    return next(c for c in commands if isinstance(c, OpenLiveConnection))


def announcement(commands) -> SendAnnouncementToLlm:
    return next(c for c in commands if isinstance(c, SendAnnouncementToLlm))


def visible_session(client_id: str = "c1") -> list:
    """Suite d'Événements amenant une Session visible (l'utilisateur parle)."""
    return [
        SessionRequested(client_id=client_id),
        LlmOpened(),
        LlmAudio(data=b"hi"),
    ]
