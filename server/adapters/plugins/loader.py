from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Callable, Mapping

from server.adapters.trace import emit
from server.app.secrets import MissingSecretError, SecretResolver
from server.core.plugins import (
    SERVER_PLATFORM,
    ConfigField,
    PluginBinding,
    PluginContext,
    PluginManifest,
    PluginRegistry,
    SecretField,
)
from server.core.ports import TraceSink
from server.core.tools import ToolNameConflict

_TRACE_SOURCE = "plugins"
_MANIFEST_FILE = "plugin.json"
_API_VERSION = 1
_SECRET_PREFIX = "ATHENA_PLUGIN"
_REQUIRED_TEXT_FIELDS = ("name", "version", "description", "platform", "entrypoint")


class PluginLoadError(RuntimeError):
    """Un plugin activé ne peut pas être chargé : le démarrage doit échouer.

    Réservée aux défauts **fatals** (manifeste, configuration, secret, import) :
    une plateforme injoignable donnera plus tard un démarrage dégradé, pas une
    erreur de chargement (ticket #57).
    """


class PluginLoader:
    """Charge les plugins serveur activés depuis le disque (ADR-0014).

    Le chargeur possède toute l'I/O du chargement : lecture du fichier d'état,
    scan du répertoire des plugins, validation du manifeste, résolution des
    secrets et import du point d'entrée. Le noyau ne reçoit qu'un
    ``PluginBinding`` agrégé dans le ``PluginRegistry`` injecté.

    Seuls les plugins **listés** dans le fichier d'état sont importés ; un
    dossier présent mais non listé est tracé ``disabled`` sans être exécuté.
    Un défaut sur un plugin activé lève ``PluginLoadError`` et fait échouer le
    démarrage ; un plugin listé dont le dossier a disparu est tracé ``missing``
    sans perdre sa ligne d'état.
    """

    def __init__(
        self,
        *,
        directory: str | Path,
        state_path: str | Path,
        secrets: SecretResolver,
        trace_sink: TraceSink,
    ) -> None:
        self._directory = Path(directory)
        self._state_path = Path(state_path)
        self._secrets = secrets
        self._trace_sink = trace_sink

    async def load(
        self,
        registry: PluginRegistry,
        *,
        push_event: Callable[[str, str], None],
    ) -> None:
        """Charge les plugins activés et trace l'état de chacun.

        L'ordre est déterministe : les plugins activés dans l'ordre du fichier
        d'état, puis les dossiers présents non listés (par nom). Une erreur sur
        un plugin activé interrompt le chargement : le démarrage échoue.
        """
        enabled, configs = self._read_state()
        for name in enabled:
            await self._load_enabled(name, registry, configs.get(name, {}), push_event)
        for name in sorted(self._on_disk() - set(enabled)):
            self._trace_state(name, "disabled")

    # -- État d'activation ---------------------------------------------------

    def _read_state(self) -> tuple[tuple[str, ...], dict[str, Mapping[str, object]]]:
        """Lit la liste d'activation et la configuration non secrète.

        Un fichier d'état absent signifie « aucun plugin activé », jamais une
        erreur. Un fichier présent mais malformé est un défaut de
        configuration : il fait échouer le démarrage avec un message explicite.
        """
        if not self._state_path.is_file():
            return (), {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginLoadError(
                f"invalid plugins state file {self._state_path}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise PluginLoadError(
                f"invalid plugins state file {self._state_path}: expected a JSON object"
            )
        raw_enabled = data.get("enabled", [])
        if not isinstance(raw_enabled, list) or not all(
            isinstance(name, str) and name.strip() for name in raw_enabled
        ):
            raise PluginLoadError(
                f"invalid plugins state file {self._state_path}: "
                "'enabled' must be a list of plugin names"
            )
        enabled = tuple(dict.fromkeys(name.strip() for name in raw_enabled))
        raw_config = data.get("config", {})
        if not isinstance(raw_config, dict):
            raise PluginLoadError(
                f"invalid plugins state file {self._state_path}: "
                "'config' must be an object"
            )
        configs: dict[str, Mapping[str, object]] = {}
        for name, values in raw_config.items():
            if not isinstance(values, dict):
                raise PluginLoadError(
                    f"invalid plugins state file {self._state_path}: "
                    f"config for plugin {name!r} must be an object"
                )
            configs[str(name)] = values
        return enabled, configs

    def _on_disk(self) -> set[str]:
        if not self._directory.is_dir():
            return set()
        return {entry.name for entry in self._directory.iterdir() if entry.is_dir()}

    # -- Chargement d'un plugin activé ---------------------------------------

    async def _load_enabled(
        self,
        name: str,
        registry: PluginRegistry,
        raw_config: Mapping[str, object],
        push_event: Callable[[str, str], None],
    ) -> None:
        plugin_dir = self._directory / name
        if not plugin_dir.is_dir():
            self._trace_state(name, "missing")
            return
        manifest = self._read_manifest(name, plugin_dir)
        if manifest.platform != SERVER_PLATFORM:
            # Une plateforme cliente n'est jamais chargée par le serveur : le
            # plugin reste inerte ici, sans faire échouer le démarrage.
            self._trace_state(name, "disabled", platform=manifest.platform)
            return
        config = self._resolve_config(manifest, raw_config)
        secrets = self._resolve_secrets(manifest)
        binding = await self._create_binding(
            name, plugin_dir, manifest, config, secrets, push_event
        )
        if binding.name != name:
            raise PluginLoadError(
                f"plugin {name!r}: create() returned a binding named "
                f"{binding.name!r}; it must match the plugin name"
            )
        try:
            registry.add(binding)
        except ToolNameConflict as exc:
            # Une collision de noms est un défaut de configuration : elle fait
            # échouer le démarrage avec le même chemin qu'un manifeste invalide.
            raise PluginLoadError(f"plugin {name!r}: {exc}") from exc
        self._trace_state(name, "activated", tools=len(binding.tools))

    def _read_manifest(self, name: str, plugin_dir: Path) -> PluginManifest:
        """Lit et valide strictement le manifeste d'un plugin activé."""
        path = plugin_dir / _MANIFEST_FILE
        if not path.is_file():
            raise PluginLoadError(f"plugin {name!r}: missing manifest {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginLoadError(
                f"plugin {name!r}: manifest is not valid JSON ({exc})"
            ) from exc
        if not isinstance(data, dict):
            raise PluginLoadError(
                f"plugin {name!r}: manifest must be a JSON object"
            )
        api_version = data.get("api_version")
        if api_version != _API_VERSION:
            raise PluginLoadError(
                f"plugin {name!r}: unsupported api_version {api_version!r} "
                f"(expected {_API_VERSION})"
            )
        fields: dict[str, str] = {}
        for field in _REQUIRED_TEXT_FIELDS:
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise PluginLoadError(
                    f"plugin {name!r}: manifest field {field!r} is missing or empty"
                )
            fields[field] = value.strip()
        manifest_name = fields["name"]
        if manifest_name != name:
            raise PluginLoadError(
                f"plugin {name!r}: manifest name {manifest_name!r} does not match "
                "its directory"
            )
        entrypoint = fields["entrypoint"]
        if not (plugin_dir / entrypoint).is_file():
            raise PluginLoadError(
                f"plugin {name!r}: entrypoint {entrypoint!r} does not exist"
            )
        return PluginManifest(
            api_version=api_version,
            name=manifest_name,
            version=fields["version"],
            description=fields["description"],
            platform=fields["platform"],
            entrypoint=entrypoint,
            config=self._read_config_fields(name, data.get("config", [])),
            secrets=self._read_secret_fields(name, data.get("secrets", [])),
        )

    @staticmethod
    def _read_entries(
        name: str, field: str, raw: object
    ) -> list[tuple[str, bool, Mapping[str, object]]]:
        """Valide la forme commune des listes ``config`` et ``secrets``.

        Chaque entrée est un objet avec une ``key`` non vide et un ``required``
        booléen optionnel (faux par défaut).
        """
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise PluginLoadError(
                f"plugin {name!r}: manifest field {field!r} must be a list"
            )
        entries: list[tuple[str, bool, Mapping[str, object]]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise PluginLoadError(
                    f"plugin {name!r}: manifest field {field!r} entries must be objects"
                )
            key = entry.get("key")
            if not isinstance(key, str) or not key.strip():
                raise PluginLoadError(
                    f"plugin {name!r}: manifest field {field!r} entry without a 'key'"
                )
            required = entry.get("required", False)
            if not isinstance(required, bool):
                raise PluginLoadError(
                    f"plugin {name!r}: 'required' for {key!r} must be a boolean"
                )
            entries.append((key.strip(), required, entry))
        return entries

    @classmethod
    def _read_config_fields(cls, name: str, raw: object) -> tuple[ConfigField, ...]:
        fields: list[ConfigField] = []
        for key, required, entry in cls._read_entries(name, "config", raw):
            value_type = entry.get("type", "string")
            if not isinstance(value_type, str) or not value_type.strip():
                raise PluginLoadError(
                    f"plugin {name!r}: 'type' for {key!r} must be a string"
                )
            fields.append(
                ConfigField(
                    key=key, type=value_type.strip(), required=required
                )
            )
        return tuple(fields)

    @classmethod
    def _read_secret_fields(cls, name: str, raw: object) -> tuple[SecretField, ...]:
        return tuple(
            SecretField(key=key, required=required)
            for key, required, _ in cls._read_entries(name, "secrets", raw)
        )

    def _resolve_config(
        self, manifest: PluginManifest, raw_config: Mapping[str, object]
    ) -> dict[str, str]:
        """Configuration non secrète déclarée par le manifeste, valeurs en texte."""
        values: dict[str, str] = {}
        for field in manifest.config:
            raw = raw_config.get(field.key)
            text = "" if raw is None else str(raw).strip()
            if not text:
                if field.required:
                    raise PluginLoadError(
                        f"plugin {manifest.name!r}: required config key "
                        f"{field.key!r} is missing from the plugins state file"
                    )
                continue
            values[field.key] = text
        return values

    def _resolve_secrets(self, manifest: PluginManifest) -> dict[str, str]:
        """Résout les secrets déclarés, sans jamais les tracer ni les écrire."""
        values: dict[str, str] = {}
        for field in manifest.secrets:
            variable = secret_variable(manifest.name, field.key)
            try:
                values[field.key] = self._secrets.resolve(variable)
            except MissingSecretError:
                if field.required:
                    raise PluginLoadError(
                        f"plugin {manifest.name!r}: required secret {variable} "
                        "is missing from the environment"
                    ) from None
        return values

    # -- Import et création du binding ---------------------------------------

    async def _create_binding(
        self,
        name: str,
        plugin_dir: Path,
        manifest: PluginManifest,
        config: Mapping[str, str],
        secrets: Mapping[str, str],
        push_event: Callable[[str, str], None],
    ) -> PluginBinding:
        """Importe le point d'entrée et fait rendre au plugin son binding.

        ``create`` peut être synchrone ou asynchrone : le serveur attend le
        résultat sans thread (une seule boucle, ADR-0005). Une exception levée
        par ``create`` est pour l'instant fatale ; le ticket #57 en fera une
        erreur retentable (démarrage dégradé), d'où le point d'entrée unique.
        """
        create = self._import_entrypoint(name, plugin_dir, manifest.entrypoint)
        context = PluginContext(
            name=name,
            config=config,
            secrets=secrets,
            trace=self._plugin_trace(name),
            push_event=push_event,
        )
        try:
            result = create(context)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            # #57 : cette branche deviendra un échec retentable et tracé
            # « failed » plutôt qu'un démarrage avorté.
            raise PluginLoadError(
                f"plugin {name!r}: create() failed ({type(exc).__name__}: {exc})"
            ) from exc
        if not isinstance(result, PluginBinding):
            raise PluginLoadError(
                f"plugin {name!r}: create() must return a PluginBinding, "
                f"got {type(result).__name__}"
            )
        return result

    def _import_entrypoint(
        self, name: str, plugin_dir: Path, entrypoint: str
    ) -> Callable[[PluginContext], object]:
        """Importe le module d'entrée et rend son ``create(context)``.

        Le dossier du plugin est ajouté à ``sys.path`` le temps de l'exécution
        pour qu'un plugin puisse importer ses propres modules voisins.
        """
        module_name = f"athena_plugin_{_sanitize(name).lower()}"
        path = plugin_dir / entrypoint
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise PluginLoadError(
                f"plugin {name!r}: cannot import entrypoint {entrypoint!r}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        sys.path.insert(0, str(plugin_dir))
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginLoadError(
                f"plugin {name!r}: importing {entrypoint!r} failed "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        finally:
            try:
                sys.path.remove(str(plugin_dir))
            except ValueError:  # pragma: no cover - défensif
                pass
        create = getattr(module, "create", None)
        if not callable(create):
            raise PluginLoadError(
                f"plugin {name!r}: entrypoint {entrypoint!r} does not define "
                "create(context)"
            )
        return create

    # -- Trace ---------------------------------------------------------------

    def _trace_state(self, plugin: str, state: str, **extra: object) -> None:
        """Trace l'état d'un plugin (source ``plugins``, kind ``PluginState``)."""
        emit(
            self._trace_sink,
            _TRACE_SOURCE,
            "PluginState",
            payload={"plugin": plugin, "state": state, **extra},
        )

    def _plugin_trace(self, name: str) -> Callable[..., None]:
        """Fonction ``trace`` injectée : source ``plugins``, plugin préfixé."""

        def trace(
            kind: str,
            payload: Mapping[str, object] | None = None,
            *,
            level: str = "info",
        ) -> None:
            emit(
                self._trace_sink,
                _TRACE_SOURCE,
                kind,
                payload={"plugin": name, **dict(payload or {})},
                level=level,
            )

        return trace


def secret_variable(plugin: str, key: str) -> str:
    """Variable d'environnement d'un secret de plugin (ADR-0014).

    Forme ``ATHENA_PLUGIN_<PLUGIN>_<SECRET>``, en majuscules, tout caractère
    non alphanumérique remplacé par ``_``.
    """
    return f"{_SECRET_PREFIX}_{_sanitize(plugin)}_{_sanitize(key)}"


def _sanitize(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", text.upper())
