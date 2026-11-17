"""Routes ``/api/plugins`` de la Console : état, activation, config, secrets.

La zone Plugins ne fait **que** lire et écrire l'état des Plugins installés :
aucune installation, aucune désinstallation, aucun rechargement à chaud
(ADR-0014, ADR-0024). L'écriture est persistée dans le fichier d'état et prend
effet au redémarrage suivant ; la réponse dit toujours si un redémarrage est
nécessaire.

Les réponses ne contiennent **jamais** une valeur de Secret : seulement le nom
de la variable attendue, ``configured`` et la date de mise à jour.
"""

from __future__ import annotations

from typing import Mapping

from aiohttp import web

from server.adapters.console.contract import ENVELOPE_VERSION
from server.adapters.console.errors import error_response
from server.adapters.plugins.admin import PluginAdmin, PluginAdminError
from server.adapters.plugins.state import PluginStateError

# Statut HTTP d'un refus de gestion des Plugins ; le reste est un 400 de
# formulaire, avec un code par champ.
_STATUS: dict[str, int] = {
    "plugin_not_found": 404,
    "plugin_manifest_invalid": 409,
    "unknown_secret": 404,
    "plugins_unavailable": 503,
}


class PluginsRoutes:
    """Handlers de la zone Plugins, sans noyau ni chargement de plugin."""

    def __init__(self, admin: PluginAdmin | None) -> None:
        self._admin = admin

    async def handle_list(self, request: web.Request) -> web.Response:
        try:
            admin = self._require_admin()
            plugins, restart_required = admin.list()
        except PluginAdminError as exc:
            return _admin_error(exc)
        except PluginStateError:
            return _state_invalid()
        return web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "plugins": plugins,
                "restart_required": restart_required,
            }
        )

    async def handle_update(self, request: web.Request) -> web.Response:
        """Persiste l'activation et la configuration non secrète d'un Plugin."""
        payload = await _json_object(request)
        if payload is None:
            return _invalid_request()
        enabled = payload.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            return error_response(
                "invalid_request",
                400,
                message="L'activation doit être un booléen.",
                fields={"enabled": "invalid_value"},
            )
        config = payload.get("config")
        if config is not None and not isinstance(config, Mapping):
            return error_response(
                "invalid_request",
                400,
                message="La configuration doit être un objet.",
                fields={"config": "invalid_value"},
            )
        try:
            admin = self._require_admin()
            row = admin.update(
                request.match_info["name"], enabled=enabled, config=config
            )
            _, restart_required = admin.list()
        except PluginAdminError as exc:
            return _admin_error(exc)
        except PluginStateError:
            return _state_invalid()
        return web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "plugin": row,
                "restart_required": restart_required,
            }
        )

    async def handle_secret_set(self, request: web.Request) -> web.Response:
        """Enregistre un Secret, en écriture seule."""
        payload = await _json_object(request)
        value = payload.get("value") if payload is not None else None
        try:
            admin = self._require_admin()
            secret = admin.set_secret(
                request.match_info["name"],
                request.match_info["key"],
                value if isinstance(value, str) else "",
            )
        except PluginAdminError as exc:
            return _admin_error(exc)
        except PluginStateError:
            return _state_invalid()
        return web.json_response(
            {"v": ENVELOPE_VERSION, "secret": secret}
        )

    async def handle_secret_delete(self, request: web.Request) -> web.Response:
        """Efface un Secret ; l'absence n'est pas une erreur."""
        try:
            admin = self._require_admin()
            secret = admin.delete_secret(
                request.match_info["name"], request.match_info["key"]
            )
        except PluginAdminError as exc:
            return _admin_error(exc)
        except PluginStateError:
            return _state_invalid()
        return web.json_response(
            {"v": ENVELOPE_VERSION, "secret": secret}
        )

    def _require_admin(self) -> PluginAdmin:
        if self._admin is None:
            raise PluginAdminError(
                "plugins_unavailable",
                "La gestion des Plugins n'est pas disponible.",
            )
        return self._admin


def _admin_error(exc: PluginAdminError) -> web.Response:
    return error_response(
        exc.code,
        _STATUS.get(exc.code, 400),
        message=exc.message,
        fields=exc.fields or None,
    )


def _state_invalid() -> web.Response:
    return error_response(
        "plugins_state_invalid",
        500,
        message="Le fichier d'état des Plugins est illisible.",
    )


def _invalid_request() -> web.Response:
    return error_response(
        "invalid_request",
        400,
        message="La demande est incomplète ou mal formée.",
    )


async def _json_object(request: web.Request) -> Mapping[str, object] | None:
    try:
        payload = await request.json()
    except ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None
