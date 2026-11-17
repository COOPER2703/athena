"""Réglages de la Console : lecture, écriture et application (ticket #174).

Les Réglages suivent l'ADR-0024 : la **valeur effective** et sa source sont
celles que le processus a réellement chargées, le **magasin** est la seule
couche que l'interface écrit, et l'application passe par un **redémarrage**.
Aucune valeur n'est appliquée à chaud : ``AppConfig`` est gelée.

Cette route ne calcule rien elle-même : la validation, les changements en
attente et la confirmation d'URL vivent dans ``server/core/settings.py``, et la
route ne fait qu'écrire dans le magasin puis rendre l'état. Le redémarrage est
une callable injectée — la racine de composition fournit ``App.request_shutdown``
en production, un enregistreur en test, ce qui évite de tuer pytest.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Mapping

from aiohttp import web

from server.adapters.console.contract import ENVELOPE_VERSION
from server.adapters.console.errors import error_response
from server.adapters.console.secrets import secret_status_json
from server.core.ports import SecretStore, SettingsStore
from server.core.settings import (
    DEFAULT_RESTART_COMMAND,
    SURFACES,
    SettingState,
    SettingsView,
    describe_settings,
    surface_url,
    validate_settings_update,
)

# Délai entre la réponse et la sortie du processus : le corps du PUT doit partir
# avant que les surfaces ne se ferment. Court, mais observable par un test.
_RESTART_DELAY = 0.1


class SettingsRoutes:
    """Routes ``/api/settings`` : ``GET`` pour lire, ``PUT`` pour appliquer."""

    def __init__(
        self,
        *,
        view: SettingsView,
        store: SettingsStore | None = None,
        secrets: SecretStore | None = None,
        restart: Callable[[], None] | None = None,
        restart_command: str = DEFAULT_RESTART_COMMAND,
    ) -> None:
        self._view = view
        self._store = store
        self._secrets = secrets
        # ``restart`` n'est fournie que si le redémarrage automatique est
        # autorisé : sa présence EST le drapeau, une seule source de vérité.
        self._restart = restart
        self._restart_command = restart_command

    async def handle_get(self, request: web.Request) -> web.Response:
        if self._store is None:
            return _unavailable()
        return web.json_response(self._payload(self._states()))

    async def handle_put(self, request: web.Request) -> web.Response:
        if self._store is None:
            return _unavailable()
        payload = await _json_object(request)
        if payload is None:
            return error_response(
                "invalid_settings",
                400,
                message="Un objet de réglages est attendu.",
                fields={"values": "Un objet de réglages est attendu."},
            )

        update, errors = validate_settings_update(payload, states=self._states())
        if errors:
            return error_response(
                "invalid_settings",
                400,
                message="Certains réglages sont invalides.",
                fields=errors,
            )
        assert update is not None  # aucune erreur ⇒ une écriture validée

        # Tout est validé avant la première écriture : un refus ne laisse
        # jamais le magasin à moitié modifié.
        for key, value in update.writes.items():
            self._store.set_value(key, value)
        for key in update.removals:
            self._store.delete_value(key)

        states = self._states()
        requested = False
        if self._restart is not None and any(state.pending for state in states):
            self._schedule_restart()
            requested = True
        return web.json_response(self._payload(states, requested=requested))

    # --- mécanique interne -------------------------------------------------

    def _states(self) -> tuple[SettingState, ...]:
        assert self._store is not None
        return describe_settings(self._view, self._store.values())

    def _schedule_restart(self) -> None:
        """Répond d'abord, sort ensuite : jamais depuis une requête en vol."""
        restart = self._restart
        assert restart is not None
        asyncio.get_running_loop().call_later(_RESTART_DELAY, restart)

    def _payload(
        self,
        states: tuple[SettingState, ...],
        *,
        requested: bool = False,
    ) -> dict[str, object]:
        pending = [state for state in states if state.pending]
        by_key = {state.key: state for state in states}
        return {
            "v": ENVELOPE_VERSION,
            "fields": {state.key: _field_json(state) for state in states},
            "pending": [
                {
                    "key": state.key,
                    "active": state.value,
                    "stored": state.pending_value,
                }
                for state in pending
            ],
            "restart_required": bool(pending),
            "restart": {
                "required": bool(pending),
                "automatic": self._restart is not None,
                "command": self._restart_command,
                "requested": requested,
            },
            # Les Surfaces dont une adresse change exigent une confirmation :
            # le front reçoit le couplage hôte/port, le schéma et l'URL active,
            # pour n'avoir qu'à recalculer l'URL proposée.
            "surfaces": [
                {
                    "name": surface.name,
                    "host": surface.host_key,
                    "port": surface.port_key,
                    "scheme": surface.scheme,
                    "url": surface_url(
                        surface,
                        by_key[surface.host_key].value,
                        by_key[surface.port_key].value,
                    ),
                }
                for surface in SURFACES
            ],
            # Les Secrets restent en écriture seule : nom, état et date, jamais
            # de valeur (ADR-0024).
            "secrets": (
                [
                    secret_status_json(status)
                    for status in self._secrets.statuses()
                ]
                if self._secrets is not None
                else []
            ),
            "secrets_unreadable": bool(
                self._secrets is not None and self._secrets.unreadable
            ),
        }


def _field_json(state: SettingState) -> dict[str, object]:
    return {
        "env": state.env,
        "kind": state.kind,
        "minimum": state.minimum,
        "maximum": state.maximum,
        "choices": list(state.choices) if state.choices else None,
        "value": state.value,
        "default": state.default,
        "source": state.source.value,
        "editable": state.editable,
        "read_only": state.read_only,
        "pending": state.pending,
        "pending_value": state.pending_value,
    }


def _unavailable() -> web.Response:
    return error_response(
        "settings_unavailable",
        503,
        message="Le magasin de réglages n'est pas disponible.",
    )


async def _json_object(request: web.Request) -> Mapping[str, object] | None:
    try:
        payload = await request.json()
    except ValueError:
        return None
    return payload if isinstance(payload, Mapping) else None
