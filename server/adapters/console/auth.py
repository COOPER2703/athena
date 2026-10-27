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
    TOKENS_PATH,
)
from server.core.ports import (
    AccessToken,
    AuthError,
    AuthService,
    AuthenticatedTicket,
    IssuedTicket,
)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Routes d'API publiques : elles doivent répondre avant que le navigateur ne
# possède un Ticket Console. ``/healthz`` est hors préfixe d'API et public.
_PUBLIC_API_PATHS = frozenset({SESSION_PATH, SETUP_PATH, LOGIN_PATH})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Ticket résolu par le middleware, réutilisé par un handler de la même requête.
_CONSOLE_TICKET = web.RequestKey[AuthenticatedTicket]("console_ticket")

# Une seule table : ajouter un code d'auth n'exige de toucher ni un statut ni
# un message à deux endroits. ``{min_length}`` est interpolé depuis le store.
_AUTH_ERRORS: dict[str, tuple[int, str]] = {
    "invalid_code": (403, "Code d'installation invalide ou déjà utilisé."),
    "already_installed": (
        409,
        "Un Compte existe déjà : l'installation est fermée.",
    ),
    "invalid_credentials": (401, "Identifiants incorrects."),
    # Le mot de passe actuel est faux : la requête est authentifiée, mais la
    # preuve demandée manque. Un 403 évite de déclencher le retour au login.
    "invalid_password": (403, "Le mot de passe actuel est incorrect."),
    "unauthorized": (401, "Ticket Console absent, expiré ou révoqué."),
    "too_many_attempts": (429, "Trop de tentatives : réessayez plus tard."),
    "weak_password": (
        400,
        "Le mot de passe doit contenir au moins {min_length} caractères.",
    ),
    "invalid_username": (400, "Le nom d'utilisateur ne peut pas être vide."),
    "invalid_token_name": (400, "Le nom du Jeton d'accès ne peut pas être vide."),
    "invalid_kind": (400, "La nature du Jeton doit être « client » ou « source »."),
    "invalid_source": (
        400,
        "Une source est obligatoire pour un Jeton « source » "
        "et interdite pour un Jeton « client ».",
    ),
    "token_not_found": (404, "Ce Jeton d'accès n'existe pas."),
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


def _origin_refused() -> web.Response:
    return _error(
        "origin_refused",
        403,
        message="Origine de la requête refusée.",
    )


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


def _clear_ticket_cookie(response: web.StreamResponse) -> None:
    """Efface le Ticket Console : le navigateur n'a plus rien à présenter."""
    response.del_cookie(
        TICKET_COOKIE,
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

    @property
    def _service(self) -> AuthService:
        """Service d'auth des routes et middlewares, garanti présent."""
        assert self._auth is not None
        return self._auth

    async def _authenticate_cookie(
        self, request: web.Request
    ) -> tuple[str | None, AuthenticatedTicket | None]:
        """Cookie du navigateur → Ticket résolu, ou ``(secret, None)``."""
        secret = request.cookies.get(TICKET_COOKIE)
        session = await self._service.authenticate_ticket(secret) if secret else None
        return secret, session

    def _unauthorized(self) -> web.Response:
        return _error(
            "unauthorized",
            401,
            message="Ticket Console absent, expiré ou révoqué.",
        )

    async def ticket_guard(self, request: web.Request) -> bool:
        """Revérifie le Ticket d'un flux déjà ouvert (#77).

        Le handshake valide le Ticket une fois ; un flux de longue durée doit
        aussi s'arrêter quand ce Ticket est révoqué ou expiré, pour que
        « déconnecter partout » coupe l'écoute en direct au lieu d'attendre la
        prochaine reconnexion. Sans service d'auth, la surface reste ouverte.
        """
        if self._auth is None:
            return True
        secret = request.cookies.get(TICKET_COOKIE)
        if secret is None:
            return False
        return await self._auth.authenticate_ticket(secret) is not None

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
        if self._service.setup_required and not is_public_path(request.path):
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
        if is_public_path(request.path):
            return await handler(request)

        secret, session = await self._authenticate_cookie(request)
        if session is None:
            return self._unauthorized()

        # Le handler réutilise ce Ticket plutôt que de le relire (logout-all).
        request[_CONSOLE_TICKET] = session
        response = await handler(request)
        if session.refreshed and not response.prepared:
            # L'expiration glissante n'a été repoussée qu'au plus une fois par
            # heure : le cookie ne suit que ces rafraîchissements.
            _set_ticket_cookie(response, secret, self._service.ticket_ttl)
        return response

    @web.middleware
    async def _origin_middleware(
        self, request: web.Request, handler: Handler
    ) -> web.StreamResponse:
        if request.method in _MUTATING_METHODS:
            origin = request.headers.get("Origin")
            # Un navigateur envoie toujours ``Origin`` sur une requête mutante :
            # son absence signale un client forgé, pas un appel légitime.
            # Comparaison stricte des hôtes ; le schéma est ignoré pour que TLS
            # terminé par le proxy ne casse pas le contrôle.
            if (
                origin is None
                or urlsplit(origin).netloc.lower() != request.host.lower()
            ):
                return _origin_refused()
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

        secret, session = await self._authenticate_cookie(request)
        response = web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "setup_required": self._service.setup_required,
                "authenticated": session is not None,
                "username": (
                    session.account.username if session is not None else None
                ),
            }
        )
        if session is not None and session.refreshed and secret is not None:
            _set_ticket_cookie(response, secret, self._service.ticket_ttl)
        return response

    async def handle_setup_get(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "setup_required": (
                    self._service.setup_required if self._auth is not None else False
                ),
            }
        )

    async def handle_setup_post(self, request: web.Request) -> web.Response:
        if self._auth is None:
            return self._auth_disabled()
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
            ticket = await self._service.install(code, username, password)
        except AuthError as error:
            return self._auth_error(error)
        return self._opened(ticket)

    async def handle_login_post(self, request: web.Request) -> web.Response:
        if self._auth is None:
            return self._auth_disabled()
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
            ticket = await self._service.login(username, password)
        except AuthError as error:
            return self._auth_error(error)
        return self._opened(ticket)

    # --- Jetons d'accès (ticket #78) ---------------------------------------

    async def handle_tokens_get(self, request: web.Request) -> web.Response:
        """Liste les Jetons : nom, préfixe, nature, dates. Jamais un secret."""
        if self._auth is None:
            return self._auth_disabled()
        tokens = await self._service.list_tokens()
        return web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "tokens": [_token_json(token) for token in tokens],
            }
        )

    async def handle_tokens_post(self, request: web.Request) -> web.Response:
        """Crée un Jeton et renvoie son secret en clair, une seule fois."""
        if self._auth is None:
            return self._auth_disabled()
        payload = await _json_object(request)
        name = payload.get("name") if payload is not None else None
        kind = payload.get("kind") if payload is not None else None
        source = payload.get("source") if payload is not None else None
        if (
            not isinstance(name, str)
            or not isinstance(kind, str)
            or (source is not None and not isinstance(source, str))
        ):
            return _error(
                "invalid_request",
                400,
                message="Corps JSON attendu : name, kind (client|source), source.",
            )
        try:
            issued = await self._service.create_token(
                name=name, kind=kind, source=source
            )
        except AuthError as error:
            return self._auth_error(error)
        return web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "token": _token_json(issued.token),
                "secret": issued.secret,
            },
            status=201,
        )

    async def handle_token_delete(self, request: web.Request) -> web.Response:
        """Révoque un Jeton individuellement ; la révocation est immédiate."""
        if self._auth is None:
            return self._auth_disabled()
        revoked = await self._service.revoke_token(request.match_info["token_id"])
        if revoked is None:
            return _error(
                "token_not_found",
                404,
                message="Ce Jeton d'accès n'existe pas.",
            )
        return web.json_response(
            {"v": ENVELOPE_VERSION, "token": _token_json(revoked)}
        )

    # --- Routes authentifiées ---------------------------------------------

    async def handle_logout(self, request: web.Request) -> web.Response:
        """Déconnecte le navigateur courant : Ticket révoqué, cookie effacé."""
        if self._auth is None:
            return self._auth_disabled()
        secret = request.cookies.get(TICKET_COOKIE)
        if secret:
            await self._service.revoke_ticket(secret)
        return self._signed_out()

    async def handle_logout_all(self, request: web.Request) -> web.Response:
        """« Déconnecter partout » : tous les Tickets du Compte sont révoqués."""
        if self._auth is None:
            return self._auth_disabled()
        # Le middleware a déjà résolu et validé ce Ticket ; le relire serait une
        # requête SQLite inutile (et une fenêtre de course de plus).
        session = request.get(_CONSOLE_TICKET)
        if session is None:
            _, session = await self._authenticate_cookie(request)
        if session is None:
            return self._unauthorized()
        await self._service.revoke_account_tickets(session.account.id)
        return self._signed_out()

    async def handle_password_post(self, request: web.Request) -> web.Response:
        """Change le mot de passe ; les autres Tickets du Compte sont révoqués."""
        if self._auth is None:
            return self._auth_disabled()
        payload = await _json_object(request)
        fields = _fields(payload, "current_password", "new_password")
        if fields is None:
            return _error(
                "invalid_request",
                400,
                message="Corps JSON attendu : current_password, new_password.",
            )
        current_password, new_password = fields
        secret = request.cookies.get(TICKET_COOKIE)
        if secret is None:
            return self._unauthorized()
        try:
            await self._service.change_password(
                secret, current_password, new_password
            )
        except AuthError as error:
            return self._auth_error(error)
        return web.json_response({"v": ENVELOPE_VERSION, "changed": True})


    # --- Traduction des échecs --------------------------------------------

    def _auth_disabled(self) -> web.Response:
        return _error(
            "auth_disabled",
            400,
            message="L'authentification n'est pas configurée.",
        )

    def _signed_out(self) -> web.Response:
        """Réponse commune des deux déconnexions : cookie effacé, plus d'identité."""
        response = web.json_response(
            {"v": ENVELOPE_VERSION, "authenticated": False}
        )
        _clear_ticket_cookie(response)
        return response

    def _opened(self, ticket: IssuedTicket) -> web.Response:
        response = web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "authenticated": True,
                "username": ticket.account.username,
            }
        )
        _set_ticket_cookie(response, ticket.secret, self._service.ticket_ttl)
        return response

    def _auth_error(self, error: AuthError) -> web.Response:
        status, message = _AUTH_ERRORS.get(
            error.code, (400, "Requête d'authentification refusée.")
        )
        return _error(
            error.code,
            status,
            message=message.replace(
                "{min_length}", str(self._service.password_min_length)
            ),
            retry_after=error.retry_after,
        )


def _token_json(token: AccessToken) -> dict[str, object]:
    """Forme publique d'un Jeton : ni secret, ni empreinte, jamais.

    ``revoked_at`` rend la révocation visible dans la liste ; le front en
    déduit l'état, et la ligne reste consultable après coup.
    """
    return {
        "id": token.id,
        "name": token.name,
        "kind": token.kind,
        "source": token.source,
        "prefix": token.prefix,
        "created_at": token.created_at,
        "last_used_at": token.last_used_at,
        "revoked_at": token.revoked_at,
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
