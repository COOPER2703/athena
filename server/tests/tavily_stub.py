"""Faux moteur Tavily en processus, pour les tests du plugin ``websearch``.

Le serveur imite les deux routes que le plugin utilise : ``GET /usage`` (sonde
de démarrage, sans crédit consommé) et ``POST /search`` (recherche). Il tourne
sous uvicorn sur une socket éphémère de la même boucle que le test, enregistre
chaque requête (méthode, chemin, porteur d'authentification, corps JSON) et
laisse le test programmer les réponses, y compris une erreur ou un retard.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, field
from typing import Any

import uvicorn

USAGE_PATH = "/usage"
SEARCH_PATH = "/search"

DEFAULT_RESULTS: tuple[dict[str, Any], ...] = (
    {
        "title": "Le Monde — Tournoi de X",
        "url": "https://example.test/tournoi",
        "content": "La finale a été remportée par l'équipe locale.",
        "score": 0.92,
    },
    {
        "title": "France Info — Résultat",
        "url": "https://example.test/resultat",
        "content": "Le match s'est achevé sur un score serré.",
        "published_date": "2026-10-26",
        "score": 0.81,
    },
)


@dataclass
class TavilyStub:
    """Moteur de test et ce que le test veut observer."""

    api_key: str = "tvly-test-key"
    host: str = "127.0.0.1"
    port: int = 0
    usage_status: int = 200
    usage: int = 42
    limit: int = 1000
    search_status: int = 200
    results: tuple[dict[str, Any], ...] = DEFAULT_RESULTS
    answer: str = ""
    delay: float = 0.0
    requests: list[dict[str, Any]] = field(default_factory=list)
    _server: uvicorn.Server | None = field(default=None, init=False, repr=False)
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _socket: socket.socket | None = field(default=None, init=False, repr=False)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def auth_tokens(self) -> list[str | None]:
        return [request["authorization"] for request in self.requests]

    @property
    def search_requests(self) -> list[dict[str, Any]]:
        return [request for request in self.requests if request["path"] == SEARCH_PATH]

    @property
    def usage_requests(self) -> list[dict[str, Any]]:
        return [request for request in self.requests if request["path"] == USAGE_PATH]

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        self.port = sock.getsockname()[1]

        config = uvicorn.Config(
            self,
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
                raise RuntimeError("the Tavily stub did not start")
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

    async def __aenter__(self) -> TavilyStub:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> bool:
        await self.stop()
        return False

    # -- Application ASGI ----------------------------------------------------

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return

        headers = {
            key.decode().lower(): value.decode() for key, value in scope["headers"]
        }
        body = await _read_body(receive)
        self.requests.append(
            {
                "method": scope["method"],
                "path": scope["path"],
                "authorization": headers.get("authorization"),
                "content_type": headers.get("content-type"),
                "body": body,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)

        if scope["path"] == USAGE_PATH:
            await _respond(
                send,
                self.usage_status,
                {
                    "usage": self.usage,
                    "limit": self.limit,
                    "key": {"usage": self.usage, "limit": self.limit},
                },
            )
            return
        if scope["path"] == SEARCH_PATH:
            await _respond(
                send,
                self.search_status,
                {
                    "query": body.get("query", ""),
                    "answer": self.answer,
                    "results": list(self.results),
                    "response_time": 0.12,
                },
            )
            return
        await _respond(send, 404, {"detail": "not found"})

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


async def _read_body(receive: Any) -> dict[str, Any]:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
    raw = b"".join(chunks)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw.decode("utf-8", "replace")}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


async def _respond(send: Any, status: int, payload: Any) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
