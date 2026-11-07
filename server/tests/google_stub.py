"""Faux serveur Google en processus, pour les tests du plugin ``google-workspace``.

Le serveur imite les routes que le plugin utilise : le point d'échange OAuth
(``POST /token``, corps ``application/x-www-form-urlencoded``), Gmail et, plus
tard, Calendar. Il tourne sous uvicorn sur une socket éphémère de la même
boucle que le test, enregistre chaque requête (méthode, chemin, requête
décodée, en-tête ``Authorization``, formulaire ou corps JSON) et laisse le test
programmer les réponses, y compris une erreur, un corps brut ou un retard.

Le point d'échange OAuth se programme par refresh token : plusieurs Comptes
Google peuvent ainsi réussir ou échouer indépendamment, sans dépendre de
l'ordre d'arrivée des échanges parallèles du démarrage.

Une route non programmée répond ``404`` avec l'erreur JSON de Google : le test
n'a donc pas à programmer les routes qu'il veut voir échouer, et une requête
inattendue se voit dans ``requests``.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs

import uvicorn

# Erreur JSON telle que les API Google la rendent sur une route inconnue.
NOT_FOUND_BODY: dict[str, Any] = {
    "error": {
        "code": 404,
        "message": "Not Found",
        "status": "NOT_FOUND",
    }
}


@dataclass(frozen=True, slots=True)
class _Route:
    """Réponse programmée pour un couple méthode + chemin exacts."""

    status: int = 200
    body: Any = None
    raw_body: bytes | None = None
    delay: float = 0.0


@dataclass(slots=True)
class _Sequence:
    """Suite de réponses : la suivante à chaque appel, la dernière ensuite."""

    responses: tuple[_Route, ...]
    calls: int = 0

    def next(self) -> _Route:
        """La réponse due à cet appel, la dernière restant stable."""
        route = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return route


@dataclass
class GoogleStub:
    """Serveur Google de test et ce que le test veut observer."""

    host: str = "127.0.0.1"
    port: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)
    _routes: dict[tuple[str, str], _Route | _Sequence] = field(
        default_factory=dict, init=False, repr=False
    )
    _token_routes: dict[str, _Route | _Sequence] = field(
        default_factory=dict, init=False, repr=False
    )
    _server: uvicorn.Server | None = field(default=None, init=False, repr=False)
    _task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _socket: socket.socket | None = field(default=None, init=False, repr=False)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def token_url(self) -> str:
        return self.base_url + "/token"

    @property
    def token_requests(self) -> list[dict[str, Any]]:
        """Les échanges OAuth reçus, dans l'ordre d'arrivée."""
        return [
            request for request in self.requests if request["path"] == "/token"
        ]

    @property
    def gmail_requests(self) -> list[dict[str, Any]]:
        """Toute requête Gmail reçue, dans l'ordre d'arrivée."""
        return [
            request
            for request in self.requests
            if request["path"].startswith("/gmail/")
        ]

    def route(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        body: Any = None,
        raw_body: bytes | None = None,
        delay: float = 0.0,
    ) -> None:
        """Programme la réponse d'une route ; un second appel la remplace."""
        self._routes[(method.upper(), path)] = _Route(
            status=status, body=body, raw_body=raw_body, delay=delay
        )

    def route_sequence(
        self, method: str, path: str, responses: Sequence[Mapping[str, Any]]
    ) -> None:
        """Programme une suite de réponses pour une route.

        Chaque appel consomme la réponse suivante ; la dernière se répète
        ensuite. Chaque item accepte ``status``, ``body``, ``raw_body`` et
        ``delay``, comme ``route``.
        """
        self._routes[(method.upper(), path)] = _sequence(responses)

    def route_token(
        self,
        refresh_token: str,
        *,
        status: int = 200,
        access_token: str = "access-token",
        expires_in: int | str = 3600,
        token_type: str = "Bearer",
        error: str | None = None,
        error_description: str | None = None,
        raw_body: bytes | None = None,
        delay: float = 0.0,
    ) -> None:
        """Programme l'échange d'un refresh token précis.

        Répondre par refresh token rend les tests multi-comptes déterministes :
        les échanges du démarrage partent en parallèle, leur ordre d'arrivée
        n'est pas garanti.
        """
        if raw_body is not None or error is not None:
            body: Any = (
                {
                    "error": error,
                    **(
                        {"error_description": error_description}
                        if error_description is not None
                        else {}
                    ),
                }
                if error is not None
                else None
            )
        else:
            body = {
                "access_token": access_token,
                "expires_in": expires_in,
                "token_type": token_type,
                "scope": "openid email",
            }
        self._token_routes[refresh_token] = _Route(
            status=status, body=body, raw_body=raw_body, delay=delay
        )

    def route_token_sequence(
        self,
        refresh_token: str,
        responses: Sequence[Mapping[str, Any]],
    ) -> None:
        """Programme plusieurs échanges successifs pour un refresh token.

        Chaque item accepte ``status``, ``access_token``, ``expires_in``,
        ``token_type``, ``error``, ``raw_body`` et ``delay``. Utile aux tests de
        rafraîchissement après ``401`` : le second échange rend un nouveau jeton.
        """
        routes: list[_Route] = []
        for item in responses:
            status = int(item.get("status", 200))
            raw_body = item.get("raw_body")
            error = item.get("error")
            if raw_body is not None:
                body: Any = None
            elif error is not None:
                body = {"error": error}
            else:
                body = {
                    "access_token": item.get("access_token", "access-token"),
                    "expires_in": item.get("expires_in", 3600),
                    "token_type": item.get("token_type", "Bearer"),
                }
            routes.append(
                _Route(
                    status=status,
                    body=body,
                    raw_body=raw_body,
                    delay=float(item.get("delay", 0.0)),
                )
            )
        self._token_routes[refresh_token] = _Sequence(
            responses=tuple(routes)
        )

    def requests_for(self, method: str, path: str) -> list[dict[str, Any]]:
        """Les requêtes reçues pour un couple méthode + chemin exacts."""
        method = method.upper()
        return [
            request
            for request in self.requests
            if request["method"] == method and request["path"] == path
        ]

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
                raise RuntimeError("the Google stub did not start")
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

    async def __aenter__(self) -> GoogleStub:
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
        raw = await _read_body(receive)
        content_type = headers.get("content-type", "")
        form = (
            parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
            if "application/x-www-form-urlencoded" in content_type
            else {}
        )
        self.requests.append(
            {
                "method": scope["method"],
                "path": scope["path"],
                "query": parse_qs(
                    scope.get("query_string", b"").decode("utf-8"),
                    keep_blank_values=True,
                ),
                "authorization": headers.get("authorization"),
                "content_type": content_type,
                "form": form,
                "body": _json_body(raw) if not form else {},
            }
        )

        route = self._route_for(scope["method"], scope["path"], form)
        if route is None:
            await _respond(send, 404, NOT_FOUND_BODY)
            return
        if isinstance(route, _Sequence):
            route = route.next()
        if route.delay:
            await asyncio.sleep(route.delay)
        if route.raw_body is not None:
            await _respond_raw(send, route.status, route.raw_body)
            return
        await _respond(send, route.status, {} if route.body is None else route.body)

    def _route_for(
        self, method: str, path: str, form: Mapping[str, list[str]]
    ) -> _Route | _Sequence | None:
        """La route programmée : l'échange OAuth se choisit par refresh token."""
        if method == "POST" and path == "/token":
            refresh = (form.get("refresh_token") or [""])[0]
            if refresh in self._token_routes:
                return self._token_routes[refresh]
        return self._routes.get((method, path))

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def _sequence(responses: Sequence[Mapping[str, Any]]) -> _Sequence:
    """Suite de réponses génériques programmée sur une route."""
    if not responses:
        raise ValueError("route_sequence demande au moins une réponse")
    return _Sequence(
        responses=tuple(
            _Route(
                status=int(item.get("status", 200)),
                body=item.get("body"),
                raw_body=item.get("raw_body"),
                delay=float(item.get("delay", 0.0)),
            )
            for item in responses
        )
    )


async def _read_body(receive: Any) -> bytes:
    """Corps brut de la requête, quel que soit son type de contenu."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
    return b"".join(chunks)


def _json_body(raw: bytes) -> dict[str, Any]:
    """Corps JSON décodé ; un corps non JSON retombe sur son texte brut."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw.decode("utf-8", "replace")}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


async def _respond(send: Any, status: int, payload: Any) -> None:
    await _respond_raw(send, status, json.dumps(payload).encode("utf-8"))


async def _respond_raw(send: Any, status: int, body: bytes) -> None:
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
