"""Faux Serveur MCP HA en processus, pour les tests du plugin Home Assistant.

Le serveur imite l'endpoint Assist de Home Assistant : Streamable HTTP
stateless, réponses JSON, prompt « Device control » en rôle ``assistant``,
Tools préfixés par leur domaine (``intent__``, ``homeassistant__``, ``script__``)
et outils qui échouent ou traînent. Il tourne sous uvicorn sur une socket
éphémère de la même boucle que le test ; ``stop`` puis ``start`` sur le même
port simule un redémarrage de Home Assistant.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp_types import ToolAnnotations

MCP_PATH = "/api/mcp/assist"
PROMPT_NAME = "Device control"
ASSIST_INSTRUCTION = (
    "When controlling Home Assistant always call the intent tools. "
    "Use the area and alias names the user speaks."
)

TURN_ON_TOOL = "intent__HassTurnOn"
LIVE_CONTEXT_TOOL = "homeassistant__GetLiveContext"
SCRIPT_TOOL = "script__movie_night"
DESTRUCTIVE_TOOL = "homeassistant__DestructiveTool"
EXPLODING_TOOL = "homeassistant__Explode"
SLOW_TOOL = "homeassistant__Slow"

TURN_ON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly name, e.g. 'kitchen light'"},
        "domain": {"type": "string", "description": "HA domain, e.g. 'light'"},
    },
    "required": ["name"],
}

NO_ARGS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


@dataclass(frozen=True, slots=True)
class StubToolSpec:
    """Ce que le stub publie sur ``tools/list`` (attendu tel quel côté plugin)."""

    name: str
    description: str
    parameters: dict[str, Any]
    annotations: ToolAnnotations | None = None


def _annotations(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
    open_world: bool,
) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    )


TOOL_SPECS: tuple[StubToolSpec, ...] = (
    StubToolSpec(
        TURN_ON_TOOL,
        "Turn on a device",
        TURN_ON_SCHEMA,
        _annotations(
            read_only=False, destructive=False, idempotent=True, open_world=True
        ),
    ),
    StubToolSpec(
        LIVE_CONTEXT_TOOL,
        "Get the live state of the house",
        NO_ARGS_SCHEMA,
        _annotations(
            read_only=True, destructive=False, idempotent=True, open_world=True
        ),
    ),
    StubToolSpec(
        SCRIPT_TOOL,
        "Run the movie night script",
        NO_ARGS_SCHEMA,
        _annotations(
            read_only=False, destructive=False, idempotent=False, open_world=True
        ),
    ),
    StubToolSpec(
        DESTRUCTIVE_TOOL,
        "Delete every exposed entity",
        NO_ARGS_SCHEMA,
        _annotations(
            read_only=False, destructive=True, idempotent=False, open_world=False
        ),
    ),
    StubToolSpec(EXPLODING_TOOL, "Always fails", NO_ARGS_SCHEMA),
    StubToolSpec(SLOW_TOOL, "Finishes after two seconds", NO_ARGS_SCHEMA),
)


@dataclass
class HaStub:
    """Serveur HA de test et ce que le test veut observer.

    ``delay`` retarde chaque requête HTTP avant de la servir : c'est le moyen
    fidèle de simuler une découverte qui dépasse son budget.
    """

    token: str = "test-token"
    host: str = "127.0.0.1"
    port: int = 0
    delay: float = 0.0
    with_prompt: bool = True
    calls: list[dict[str, Any]] = field(default_factory=list)
    auth_tokens: list[str | None] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    requests: list[dict[str, str]] = field(default_factory=list)
    _server: uvicorn.Server | None = field(default=None, init=False, repr=False)
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _socket: socket.socket | None = field(default=None, init=False, repr=False)

    @property
    def base_url(self) -> str:
        """URL de l'instance, sans le chemin MCP (celui que porte l'état)."""
        return f"http://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return self.base_url + MCP_PATH

    async def start(self) -> None:
        server = _build_server(self)
        app = _Observe(
            server.streamable_http_app(
                streamable_http_path=MCP_PATH,
                stateless_http=True,
                json_response=True,
                host=self.host,
            ),
            self,
        )

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        self.port = sock.getsockname()[1]

        config = uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            lifespan="on",
            timeout_graceful_shutdown=0.5,
        )
        self._server = uvicorn.Server(config)
        self._socket = sock
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not self._server.started:
            if self._server.should_exit or loop.time() > deadline:
                raise RuntimeError("the Home Assistant stub did not start")
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        server, task, sock = self._server, self._task, self._socket
        self._server = self._task = self._socket = None
        if server is None or task is None or sock is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 5.0)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            sock.close()

    async def __aenter__(self) -> HaStub:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> bool:
        await self.stop()
        return False


class _Observe:
    """Wrapper ASGI : compte les requêtes et relève le porteur d'auth."""

    def __init__(self, app: Any, stub: HaStub) -> None:
        self._app = app
        self._stub = stub

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {
                key.decode().lower(): value.decode()
                for key, value in scope["headers"]
            }
            self._stub.auth_tokens.append(headers.get("authorization"))
            self._stub.requests.append(
                {"method": scope["method"], "path": scope["path"]}
            )
            if self._stub.delay:
                await asyncio.sleep(self._stub.delay)
        await self._app(scope, receive, send)


class _RecordMCP:
    """Middleware MCP : garde la trace des méthodes JSON-RPC reçues."""

    def __init__(self, stub: HaStub) -> None:
        self._stub = stub

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        self._stub.methods.append(ctx.method)
        return await call_next(ctx)


def _record(stub: HaStub, tool: str, **arguments: Any) -> None:
    stub.calls.append({"tool": tool, "arguments": arguments})


def _build_server(stub: HaStub) -> MCPServer:
    async def turn_on(name: str = "", domain: str = "") -> str:
        _record(stub, TURN_ON_TOOL, name=name, domain=domain)
        return f"Turned on {name}"

    async def live_context() -> str:
        _record(stub, LIVE_CONTEXT_TOOL)
        return "The garage door is closed."

    async def movie_night() -> str:
        _record(stub, SCRIPT_TOOL)
        return "Started script movie_night"

    async def destructive() -> str:
        _record(stub, DESTRUCTIVE_TOOL)
        return "Deleted every exposed entity"

    async def explode() -> str:
        raise ToolError("Home Assistant refused: entity not found")

    async def slow() -> str:
        await asyncio.sleep(2.0)
        return "late"

    handlers = {
        TURN_ON_TOOL: turn_on,
        LIVE_CONTEXT_TOOL: live_context,
        SCRIPT_TOOL: movie_night,
        DESTRUCTIVE_TOOL: destructive,
        EXPLODING_TOOL: explode,
        SLOW_TOOL: slow,
    }
    tools: list[Tool] = []
    for spec in TOOL_SPECS:
        built = Tool.from_function(
            handlers[spec.name],
            name=spec.name,
            description=spec.description,
            annotations=spec.annotations,
            structured_output=False,
        )
        # Le schéma publié est celui que le test attend, pas celui des hints.
        built.parameters = spec.parameters
        tools.append(built)

    server = MCPServer(
        "fake-ha",
        version="0.0.0",
        tools=tools,
        middleware=[_RecordMCP(stub)],
    )

    @server.prompt(name=PROMPT_NAME, description="Default prompt for Home Assistant")
    def assist() -> list[dict[str, str]]:
        # HA répond en rôle ``assistant`` : le stub le reproduit exactement.
        return [{"role": "assistant", "content": ASSIST_INSTRUCTION}]

    if not stub.with_prompt:
        server.remove_prompt(PROMPT_NAME)

    return server
