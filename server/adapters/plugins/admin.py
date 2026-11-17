"""Gestion des Plugins installés : liste, activation, configuration, secrets.

L'ADR-0014 fait du fichier d'état la seule source de l'activation, et l'ADR-0024
impose que rien ne soit rechargé à chaud : ce module **écrit** le fichier et ne
charge, n'importe et n'active aucun plugin. Une activation ou une configuration
prend effet au redémarrage suivant, et l'écart entre le fichier et l'état lu au
démarrage est ce que la Console appelle « changements en attente ».

Les Secrets sont en écriture seule (ADR-0024) : ce module n'expose jamais une
valeur, seulement le nom de la variable attendue, ``configured`` et la date de
mise à jour. Une valeur de configuration inconnue ou invalide est refusée avec
un code par champ, jamais en tuant le serveur.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from plugin_api import ConfigField, SecretField
from server.adapters.plugins.catalogue import CatalogueEntry, PluginCatalogue
from server.adapters.plugins.loader import (
    PluginLoader,
    is_http_url,
    secret_variable,
)
from server.adapters.plugins.state import PluginState, PluginStateStore
from server.core.plugins import PluginDescription, PluginRegistry
from server.core.ports import SecretStatus, SecretStore

#: Une valeur de Secret plus longue est presque sûrement une erreur de copie.
MAX_SECRET_LENGTH = 8192


class PluginAdminError(RuntimeError):
    """Refus stable de la gestion des Plugins, traduit en erreur d'API.

    ``code`` est le contrat (le front le traduit en phrase), ``fields`` porte
    des **codes** par champ de formulaire. Aucun message ne cite une valeur.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        fields: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = dict(fields or {})


class PluginAdmin:
    """Service de gestion des Plugins, sans jamais recharger un plugin."""

    def __init__(
        self,
        *,
        catalogue: PluginCatalogue,
        state: PluginStateStore,
        registry: PluginRegistry,
        loader: PluginLoader,
        secrets: SecretStore | None = None,
    ) -> None:
        self._catalogue = catalogue
        self._state = state
        self._registry = registry
        self._loader = loader
        self._secrets = secrets

    # -- Lecture -------------------------------------------------------------

    def list(self) -> tuple[list[dict[str, object]], bool]:
        """Tous les Plugins connus, et l'exigence d'un redémarrage."""
        self._adopt_catalogue()
        persisted = self._state.read()
        statuses = self._secret_statuses()
        rows = [
            self._row(name, persisted, statuses)
            for name in self._known_names(persisted)
        ]
        return rows, self._restart_required(persisted, rows)

    def describe(self, name: str) -> dict[str, object]:
        """Une ligne de Plugin, pour la réponse d'une écriture."""
        self._adopt_catalogue()
        return self._row(name, self._state.read(), self._secret_statuses())

    # -- Écriture ------------------------------------------------------------

    def update(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        config: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Persiste l'activation et/ou la configuration non secrète.

        Une configuration est validée contre les champs **déclarés** par le
        manifeste : une clé inconnue, une URL invalide ou une valeur requise
        vide est refusée par champ, et rien n'est écrit. Les clés de
        configuration qu'un opérateur aurait ajoutées à la main ne sont pas
        effacées. Désactiver un Plugin dont le dossier a disparu reste possible :
        une configuration, elle, exige un manifeste lisible.
        """
        self._adopt_catalogue()
        persisted = self._state.read()
        if name not in self._known_names(persisted):
            raise PluginAdminError(
                "plugin_not_found",
                "Ce Plugin n'existe pas dans le répertoire des Plugins.",
            )
        values: dict[str, str] | None = None
        if config is not None:
            manifest = self._require_manifest(name)
            if not isinstance(config, Mapping):
                raise PluginAdminError(
                    "invalid_plugin_config",
                    "La configuration doit être un objet.",
                    fields={"config": "invalid_value"},
                )
            values = self._validated_config(manifest.config, persisted, name, config)
        if enabled is None and values is None:
            return self.describe(name)
        self._state.update(name, enabled=enabled, config=values)
        return self.describe(name)

    def set_secret(self, name: str, key: str, value: str) -> dict[str, object]:
        """Chiffre un Secret de Plugin ; aucune réponse ne reprend la valeur."""
        field = self._require_secret(name, key)
        if not isinstance(value, str) or not value.strip():
            raise PluginAdminError(
                "invalid_plugin_secret",
                "Une valeur est attendue.",
                fields={"value": "required"},
            )
        if len(value) > MAX_SECRET_LENGTH:
            raise PluginAdminError(
                "invalid_plugin_secret",
                "Cette valeur est trop longue.",
                fields={"value": "too_long"},
            )
        status = self._store().set_secret(secret_variable(name, key), value)
        return _secret_json(name, field, status)

    def delete_secret(self, name: str, key: str) -> dict[str, object]:
        """Efface un Secret de Plugin ; l'absence n'est pas une erreur."""
        field = self._require_secret(name, key)
        store = self._store()
        store.delete_secret(secret_variable(name, key))
        return _secret_json(name, field, None)

    # -- Internes ------------------------------------------------------------

    def _store(self) -> SecretStore:
        if self._secrets is None:
            raise PluginAdminError(
                "plugins_unavailable",
                "Le magasin de Secrets n'est pas disponible.",
            )
        return self._secrets

    def _adopt_catalogue(self) -> None:
        """Verse les métadonnées du catalogue dans le registre, une seule fois.

        Le registre reste ainsi la source unique de ce que la Console décrit :
        un dossier non listé ou au manifeste cassé y entre aussi, sans que le
        chargeur ait eu à l'ouvrir.
        """
        for entry in self._catalogue.entries():
            if self._registry.description_of(entry.name) is None:
                self._registry.describe(entry.description)

    def _require_manifest(self, name: str):
        entry = self._catalogue.entry(name)
        if entry is None:
            raise PluginAdminError(
                "plugin_not_found",
                "Ce Plugin n'existe pas dans le répertoire des Plugins.",
            )
        if entry.manifest is None:
            raise PluginAdminError(
                "plugin_manifest_invalid",
                "Le manifeste de ce Plugin est illisible : corrigez-le sur le serveur.",
            )
        return entry.manifest

    def _require_secret(self, name: str, key: str) -> SecretField:
        manifest = self._require_manifest(name)
        for field in manifest.secrets:
            if field.key == key:
                return field
        raise PluginAdminError(
            "unknown_secret",
            "Ce Plugin ne déclare pas ce Secret.",
        )

    def _validated_config(
        self,
        fields: Sequence[ConfigField],
        persisted: PluginState,
        name: str,
        provided: Mapping[str, object],
    ) -> dict[str, str]:
        """Résout et valide la configuration soumise, sans rien écrire."""
        existing = persisted.config.get(name, {})
        values: dict[str, str] = {
            str(key): text
            for key, text in existing.items()
            if isinstance(text, str)
        }
        errors: dict[str, str] = {}
        declared = {field.key for field in fields}
        for key in provided:
            if key not in declared:
                errors[str(key)] = "unknown_field"
        for field in fields:
            raw = provided.get(field.key, existing.get(field.key, ""))
            if not isinstance(raw, str):
                errors[field.key] = "invalid_value"
                continue
            text = raw.strip()
            if field.required and not text:
                errors[field.key] = "required"
            elif field.type == "url" and text and not is_http_url(text):
                errors[field.key] = "invalid_url"
            values[field.key] = text
        if errors:
            raise PluginAdminError(
                "invalid_plugin_config",
                "La configuration du Plugin est refusée.",
                fields=errors,
            )
        return values

    def _known_names(self, persisted: PluginState) -> list[str]:
        names = {entry.name for entry in self._catalogue.entries()}
        names.update(self._registry.states)
        names.update(persisted.enabled)
        names.update(persisted.config)
        return sorted(names)

    def _row(
        self,
        name: str,
        persisted: PluginState,
        statuses: Mapping[str, SecretStatus],
    ) -> dict[str, object]:
        entry: CatalogueEntry | None = self._catalogue.entry(name)
        description = self._registry.description_of(name)
        if description is None and entry is not None:
            description = entry.description
        if description is None:
            description = PluginDescription(name=name)
        enabled = name in persisted.enabled
        state = self._registry.state_of(name) or (
            "missing" if enabled else "disabled"
        )
        stored = persisted.config.get(name, {})
        return {
            "name": name,
            "enabled": enabled,
            "state": state,
            "version": description.version,
            "platform": description.platform,
            "description": description.description,
            "tools": list(self._registry.tools_of(name)),
            "instruction": self._registry.instruction_of(name),
            "config": [
                {
                    "key": field.key,
                    "type": field.type,
                    "required": field.required,
                    "value": _text(stored.get(field.key)),
                }
                for field in description.config
            ],
            "secrets": [
                _secret_json(name, field, statuses.get(secret_variable(name, field.key)))
                for field in description.secrets
            ],
            "pending_restart": self._pending_restart(
                name, description, persisted, enabled
            ),
            "error": description.error,
        }

    def _pending_restart(
        self,
        name: str,
        description: PluginDescription,
        persisted: PluginState,
        enabled: bool,
    ) -> bool:
        """Vrai si le fichier diffère de l'état chargé au démarrage."""
        loaded = self._loader.state_at_load
        if loaded is None:
            return False
        if enabled != (name in loaded.enabled):
            return True
        current = persisted.config.get(name, {})
        before = loaded.config.get(name, {})
        for field in description.config:
            if _text(current.get(field.key)) != _text(before.get(field.key)):
                return True
        return False

    def _restart_required(
        self, persisted: PluginState, rows: Sequence[Mapping[str, object]]
    ) -> bool:
        loaded = self._loader.state_at_load
        if loaded is None:
            return False
        if set(persisted.enabled) != set(loaded.enabled):
            return True
        return any(bool(row["pending_restart"]) for row in rows)

    def _secret_statuses(self) -> Mapping[str, SecretStatus]:
        if self._secrets is None:
            return {}
        return {status.name: status for status in self._secrets.statuses()}


def _text(value: object) -> str:
    """Valeur de configuration telle que le formulaire l'affiche."""
    return "" if value is None else str(value)


def _secret_json(
    plugin: str, field: SecretField, status: SecretStatus | None
) -> dict[str, object]:
    """État d'un Secret : nom de variable, jamais la valeur (ADR-0024)."""
    return {
        "key": field.key,
        "required": field.required,
        "variable": secret_variable(plugin, field.key),
        "configured": bool(status is not None and status.configured),
        "updated_at": status.updated_at if status is not None else None,
    }
