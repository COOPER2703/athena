"""Ticket Console : middlewares d'authentification et routes d'identité.

La Console est une surface publique (spec #75) : la page HTML, ses assets,
``/healthz``, ``/api/session``, ``/api/setup`` et ``/api/login`` restent
accessibles sans preuve d'identité. Tout le reste exige un **Ticket Console**,
matérialisé par un cookie ``HttpOnly; Secure; SameSite=Strict``.

Ordre des middlewares, imposé par la spec : installation, puis Ticket, puis
``Origin`` pour les méthodes mutantes. Une instance non installée répond donc
``409 setup_required`` même sans Ticket ; une instance installée répond ``401``
en JSON à toute route d'API inconnue, jamais une page HTML.

Ce module ne connaît que le port ``AuthService`` du noyau : ni SQL, ni Argon2.
"""

from __future__ import annotations

import math
from typing import Awaitable, Callable, Mapping
from urllib.parse import urlsplit

from aiohttp import web

from server.adapters.console.contract import (
    API_PREFIX,
    ENVELOPE_VERSION,
    HEALTH_PATH,
    LOGIN_PATH,
    SESSION_PATH,
    SETUP_PATH,
    TICKET_COOKIE,
)
from server.core.ports import AuthError, AuthService, IssuedTicket

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Routes d'API publiques : elles doivent répondre avant que le navigateur ne
# possède un Ticket Console. ``/healthz`` est hors préfixe d'API et public.
_PUBLIC_API_PATHS = frozenset({SESSION_PATH, SETUP_PATH, LOGIN_PATH})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_STATUS_BY_CODE = {
    "invalid_code": 403,
    "already_installed": 409,
    "invalid_credentials": 401,
    "too_many_attempts": 429,
    "weak_password": 400,
    "invalid_username": 400,
}


def is_api_path(path: str) -> bool:
    """Vrai pour ``/api`` et tout ce qui vit dessous (le repli JSON)."""
    return path == API_PREFIX or path.startswith(f"{API_PREFIX}/")


def is_public_path(path: str) -> bool:
    """Routes servies sans identité : SPA, assets, santé, identité, login."""
    if path == HEALTH_PATH or path in _PUBLIC_API_PATHS:
        return True
    # Tout ce qui n'est pas de l'API appartient à la SPA : elle doit pouvoir
    # s'afficher pour montrer l'écran de connexion.
    return not is_api_path(path)


def _error(
    code: str,
    status: int,
    *,
    message: str,
    retry_after: float | None = None,
) -> web.Response:
    body: dict[str, object] = {
        "v": ENVELOPE_VERSION,
        "error": code,
        "message": message,
    }
    headers: dict[str, str] = {}
    if retry_after is not None:
        seconds = max(1, math.ceil(retry_after))
        body["retry_after"] = seconds
        headers["Retry-After"] = str(seconds)
    return web.json_response(body, status=status, headers=headers)


def _set_ticket_cookie(
    response: web.StreamResponse, secret: str, ttl: int
) -> None:
    """Pose le Ticket Console : jamais lisible en JS, jamais en clair sur HTTP."""
    response.set_cookie(
        TICKET_COOKIE,
        secret,
        max_age=ttl,
        path="/",
        httponly=True,
        secure=True,
        samesite="Strict",
    )


async def _json_object(request: web.Request) -> Mapping[str, object] | None:
    try:
        payload = await request.json()
    except ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None


class ConsoleAuth:
    """Couture HTTP entre la Console et le port ``AuthService``.

    ``auth=None`` garde la surface ouverte : c'est le mode des tests unitaires
    de l'adaptateur et des déploiements sans module d'auth. La racine de
    composition injecte toujours un vrai service, donc la production est close.
    """

    def __init__(self, auth: AuthService | None) -> None:
        self._auth = auth

    # --- Middlewares -------------------------------------------------------

    def middlewares(self) -> list[Callable[..., Awaitable[web.StreamResponse]]]:
        if self._auth is None:
            return []
        return [
            self._setup_middleware,
            self._ticket_middleware,
            self._origin_middleware,
        ]

    @web.middleware
    async def _setup_middleware(
        self, request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        assert self._auth is not None
        if self._auth.setup_required and not is_public_path(request.path):
            return _error(
                "setup_required",
                409,
                message="L'instance n'est pas encore installée.",
            )
        return await handler(request)

    @web.middleware
    async def _ticket_middleware(
        self, request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        assert self._auth is not None
        if is_public_path(request.path):
            return await handler(request)

        secret = request.cookies.get(TICKET_COOKIE)
        session = (
            await self._auth.authenticate_ticket(secret) if secret else None
        )
        if session is None:
            return _error(
                "unauthorized",
                401,
                message="Ticket Console absent, expiré ou révoqué.",
            )

        response = await handler(request)
        if session.refreshed and not response.prepared:
            # L'expiration glissante n'a été repoussée qu'au plus une fois par
            # heure : le cookie ne suit que ces rafraîchissements.
            _set_ticket_cookie(response, secret, self._auth.ticket_ttl)
        return response

    @web.middleware
    async def _origin_middleware(
        self, request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        if request.method in _MUTATING_METHODS:
            origin = request.headers.get("Origin")
            if origin is not None:
                # Comparaison stricte des hôtes ; le schéma est ignoré pour que
                # TLS terminé par le proxy ne casse pas le contrôle.
                if urlsplit(origin).netloc.lower() != request.host.lower():
                    return _error(
                        "origin_refused",
                        403,
                        message="Origine de la requête refusée.",
                    )
        return await handler(request)

    # --- Routes publiques --------------------------------------------------

    async def handle_health(self, request: web.Request) -> web.Response:
        """Sonde fixe : aucun instantané, aucune Trace, aucun secret."""
        return web.json_response({"status": "ok"})

    async def handle_session(self, request: web.Request) -> web.Response:
        if self._auth is None:
            return web.json_response(
                {
                    "v": ENVELOPE_VERSION,
                    "setup_required": False,
                    "authenticated": True,
                    "username": None,
                }
            )

        authenticated = False
        username: str | None = None
        refreshed = False
        secret = request.cookies.get(TICKET_COOKIE)
        if secret:
            session = await self._auth.authenticate_ticket(secret)
            if session is not None:
                authenticated = True
                username = session.account.username
                refreshed = session.refreshed
        response = web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "setup_required": self._auth.setup_required,
                "authenticated": authenticated,
                "username": username,
            }
        )
        if refreshed and secret is not None:
            _set_ticket_cookie(response, secret, self._auth.ticket_ttl)
        return response

    async def handle_setup_get(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "setup_required": (
                    self._auth.setup_required if self._auth is not None else False
                ),
            }
        )

    async def handle_setup_post(self, request: web.Request) -> web.Response:
        if self._auth is None:
            return _error(
                "auth_disabled",
                400,
                message="L'authentification n'est pas configurée.",
            )
        payload = await _json_object(request)
        fields = _fields(payload, "code", "username", "password")
        if fields is None:
            return _error(
                "invalid_request",
                400,
                message="Corps JSON attendu : code, username, password.",
            )
        code, username, password = fields
        try:
            ticket = await self._auth.install(code, username, password)
        except AuthError as error:
            return self._auth_error(error)
        return self._opened(ticket)

    async def handle_login_post(self, request: web.Request) -> web.Response:
        if self._auth is None:
            return _error(
                "auth_disabled",
                400,
                message="L'authentification n'est pas configurée.",
            )
        payload = await _json_object(request)
        fields = _fields(payload, "username", "password")
        if fields is None:
            return _error(
                "invalid_request",
                400,
                message="Corps JSON attendu : username, password.",
            )
        username, password = fields
        try:
            ticket = await self._auth.login(username, password)
        except AuthError as error:
            return self._auth_error(error)
        return self._opened(ticket)

    # --- Traduction des échecs --------------------------------------------

    def _opened(self, ticket: IssuedTicket) -> web.Response:
        assert self._auth is not None
        response = web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "authenticated": True,
                "username": ticket.account.username,
            }
        )
        _set_ticket_cookie(response, ticket.secret, self._auth.ticket_ttl)
        return response

    def _auth_error(self, error: AuthError) -> web.Response:
        assert self._auth is not None
        status = _STATUS_BY_CODE.get(error.code, 400)
        message = _MESSAGE_BY_CODE.get(error.code, "Requête d'authentification refusée.")
        if error.code == "weak_password":
            message = (
                "Le mot de passe doit contenir au moins "
                f"{self._auth.password_min_length} caractères."
            )
        return _error(
            error.code,
            status,
            message=message,
            retry_after=error.retry_after,
        )


_MESSAGE_BY_CODE = {
    "invalid_code": "Code d'installation invalide ou déjà utilisé.",
    "already_installed": "Un Compte existe déjà : l'installation est fermée.",
    "invalid_credentials": "Identifiants incorrects.",
    "too_many_attempts": "Trop de tentatives : réessayez plus tard.",
    "invalid_username": "Le nom d'utilisateur ne peut pas être vide.",
}


def _fields(payload: Mapping[str, object] | None, *names: str) -> tuple[str, ...] | None:
    """Extrait des champs texte ; tout corps incomplet est refusé en bloc."""
    if payload is None:
        return None
    values: list[str] = []
    for name in names:
        value = payload.get(name)
        if not isinstance(value, str):
            return None
        values.append(value)
    return tuple(values)
