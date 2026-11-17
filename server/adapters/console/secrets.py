"""Secrets de la Console : noms et états en lecture, valeur jamais relue.

Les routes sont en **écriture seule** (ADR-0024) : ``GET`` ne rend que le nom,
``configured`` et la date de mise à jour ; ``PUT`` valide puis chiffre ;
``DELETE`` efface. Aucune réponse, aucune trace, aucun message d'erreur ne
contient la valeur saisie.

La validation distante est injectée (``SecretValidator``) : seule la clé Gemini
sait être vérifiée par un appel réel, et les tests n'atteignent jamais le réseau.
"""

from __future__ import annotations

import re
from typing import Mapping

from aiohttp import web

from server.adapters.console.contract import ENVELOPE_VERSION
from server.adapters.console.errors import error_response
from server.core.ports import SecretValidator, SecretStatus, SecretStore

# Nom d'un Secret = nom de la variable d'environnement attendue. La forme est
# bornée pour qu'une faute de frappe ne crée pas une ligne inutilisable.
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_MAX_SECRET_LENGTH = 8192


class SecretsRoutes:
    """Routes ``/api/secrets`` de la Console (ticket #170)."""

    def __init__(
        self,
        *,
        secrets: SecretStore | None,
        validator: SecretValidator | None = None,
    ) -> None:
        self._secrets = secrets
        self._validator = validator

    async def handle_list(self, request: web.Request) -> web.Response:
        if self._secrets is None:
            return _unavailable()
        secrets = self._secrets
        return web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "secrets": [_secret_json(status) for status in secrets.statuses()],
                "secrets_unreadable": secrets.unreadable,
            }
        )

    async def handle_set(self, request: web.Request) -> web.Response:
        if self._secrets is None:
            return _unavailable()
        name = request.match_info["name"]
        if _SECRET_NAME.match(name) is None:
            return error_response(
                "invalid_secret_name",
                400,
                message="Le nom d'un Secret doit être une variable en majuscules.",
                fields={"name": "Nom de Secret invalide."},
            )
        payload = await _json_object(request)
        value = payload.get("value") if payload is not None else None
        if not isinstance(value, str) or not value.strip():
            return _invalid_value("Une valeur est attendue.")
        if len(value) > _MAX_SECRET_LENGTH:
            return _invalid_value("Cette valeur est trop longue.")

        message = await self._validate(name, value)
        if message is not None:
            return _invalid_value(message)

        status = self._secrets.set_secret(name, value)
        return web.json_response(
            {"v": ENVELOPE_VERSION, "secret": _secret_json(status)}
        )

    async def handle_delete(self, request: web.Request) -> web.Response:
        if self._secrets is None:
            return _unavailable()
        name = request.match_info["name"]
        if not self._secrets.delete_secret(name):
            return error_response(
                "secret_not_found",
                404,
                message="Ce Secret n'existe pas.",
            )
        return web.json_response(
            {
                "v": ENVELOPE_VERSION,
                "secret": _secret_json(
                    SecretStatus(name=name, configured=False, updated_at=None)
                ),
            }
        )

    async def _validate(self, name: str, value: str) -> str | None:
        if self._validator is None:
            return None
        try:
            return await self._validator.validate(name, value)
        except Exception:
            # La valeur n'apparaît jamais : le détail de la panne distante
            # appartient au validateur, pas au message rendu à l'utilisateur.
            return (
                "La vérification du Secret a échoué : réessayez dans un instant."
            )


def _secret_json(status: SecretStatus) -> dict[str, object]:
    return {
        "name": status.name,
        "configured": status.configured,
        "updated_at": status.updated_at,
    }


def _invalid_value(message: str) -> web.Response:
    return error_response(
        "invalid_secret",
        400,
        message=message,
        fields={"value": message},
    )


def _unavailable() -> web.Response:
    return error_response(
        "secrets_unavailable",
        503,
        message="Le magasin de Secrets n'est pas disponible.",
    )


async def _json_object(request: web.Request) -> Mapping[str, object] | None:
    try:
        payload = await request.json()
    except ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None
