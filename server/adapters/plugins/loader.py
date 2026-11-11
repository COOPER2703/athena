from __future__ import annotations

import importlib.util
import inspect
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import urlsplit

from server.adapters.trace import emit
from server.core.plugins import (
    KNOWN_PLATFORMS,
    SERVER_PLATFORM,
    ConfigField,
    ConfigValue,
    PluginBinding,
    PluginContext,
    PluginDeclaration,
    PluginManifest,
    PluginObservation,
    PluginRegistry,
    PluginUnavailable,
    SecretField,
    SecretPresence,
)
from server.core.ports import MissingSecretError, SecretResolver, TraceSink
from server.core.tools import ToolNameConflict

_TRACE_SOURCE = "plugins"
_MANIFEST_FILE = "plugin.json"
_API_VERSION = 1
_SECRET_PREFIX = "ATHENA_PLUGIN"
_REQUIRED_TEXT_FIELDS = ("name", "version", "description", "platform", "entrypoint")
# Types de configuration que le chargeur sait valider (ADR-0014). En déclarer
# un autre est un défaut fatal du manifeste : mieux vaut refuser que passer une
# valeur non validée au plugin.
_CONFIG_FIELD_TYPES = ("string", "url")
_REDACTED = "***"


class PluginLoadError(RuntimeError):
    """Un plugin activé ne peut pas être chargé : le démarrage doit échouer.

    Réservée aux défauts **fatals** et permanents : manifeste, ``api_version``,
    configuration ou secret requis, import du point d'entrée, ``create()`` qui
    lève autre chose que ``PluginUnavailable`` (bogue du plugin), collision de
    Tools. Seule ``PluginUnavailable`` (plateforme injoignable) donne un
    démarrage dégradé et une retente en tâche de fond (#57).
    """


class _CreateFailure(Exception):
    """``create(context)`` a levé ``PluginUnavailable`` : panne retentable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class _PendingPlugin:
    """Un plugin activé dont ``create`` a échoué, prêt à être retenté.

    Le callable d'entrée est conservé pour ne pas réimporter le module à chaque
    tentative : ``create`` est rappelé sur le module déjà chargé, ce qui garde
    l'état interne du plugin (son compteur de tentatives, son cache).
    """

    name: str
    create: Callable[[PluginContext], object]
    config: Mapping[str, str]
    secrets: Mapping[str, str]


class PluginLoader:
    """Charge les plugins serveur activés depuis le disque (ADR-0014).

    Le chargeur possède toute l'I/O du chargement : lecture du fichier d'état,
    scan du répertoire des plugins, validation du manifeste, résolution des
    secrets et import du point d'entrée. Le noyau ne reçoit qu'un
    ``PluginBinding`` agrégé dans le ``PluginRegistry`` injecté.

    Seuls les plugins **listés** dans le fichier d'état sont importés. Chaque
    dossier du répertoire est néanmoins lu au démarrage pour sa Déclaration :
    un dossier présent mais non listé est tracé ``inactive`` sans être exécuté,
    un plugin d'une autre plateforme ``unsupported``, et un manifeste cassé
    ``invalid`` (avec sa raison) sans interrompre le démarrage. Un défaut
    permanent d'un plugin activé lève ``PluginLoadError`` et fait échouer le
    démarrage ; une exception ``PluginUnavailable`` de ``create(context)``
    (plateforme injoignable) est tracée ``failed`` et le plugin reste en attente
    de retente (``retry_pending``), pour qu'une plateforme injoignable ne rende
    pas Athena muette. Un plugin listé dont le dossier a disparu est tracé
    ``missing`` sans perdre sa ligne d'état : le chargeur ne fait que lire le
    fichier d'état.
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
        self._pending: dict[str, _PendingPlugin] = {}

    @property
    def pending(self) -> tuple[str, ...]:
        """Plugins dont ``create`` a échoué et qui attendent une retente."""
        return tuple(self._pending)

    async def load(
        self,
        registry: PluginRegistry,
        *,
        push_event: Callable[[str, str], None],
    ) -> None:
        """Charge les plugins activés et trace l'état de chacun.

        L'ordre est déterministe : les plugins activés dans l'ordre du fichier
        d'état, puis les dossiers présents non listés (par nom). Un défaut
        permanent d'un plugin activé interrompt le chargement et fait échouer le
        démarrage ; une panne de ``create`` est tracée ``failed`` et laisse le
        chargement continuer, le plugin restant en attente de retente.
        """
        enabled, configs = self._read_state()
        for name in enabled:
            await self._load_enabled(name, registry, configs.get(name, {}), push_event)
        # Le balayage ne lit que les manifestes : aucun module d'un plugin non
        # retenu n'est importé, donc déposer un dossier n'exécute rien.
        for name in sorted(self._on_disk() - set(enabled)):
            self._scan_installed(name, registry, configs.get(name, {}))

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
            self._record_state(registry, name, "missing")
            return
        manifest = self._read_manifest(name, plugin_dir)
        registry.declare(name, _declaration(manifest, plugin_dir))
        registry.observe(
            name,
            self._observe(name, manifest.config, manifest.secrets, raw_config),
        )
        if manifest.platform != SERVER_PLATFORM:
            # Une plateforme cliente n'est jamais chargée par le serveur : le
            # plugin reste inerte ici, sans faire échouer le démarrage.
            self._record_state(
                registry, name, "unsupported", platform=manifest.platform
            )
            return
        config = self._resolve_config(manifest, raw_config)
        secrets = self._resolve_secrets(manifest)
        create = self._import_entrypoint(name, plugin_dir, manifest.entrypoint)
        try:
            binding = await self._bind(name, create, config, secrets, push_event)
        except _CreateFailure as failure:
            # Une panne de plateforme est réparable : le serveur démarre
            # dégradé et la retente prend le relais (ticket #57).
            self._pending[name] = _PendingPlugin(
                name=name, create=create, config=config, secrets=secrets
            )
            self._record_state(registry, name, "failed", reason=failure.reason)
            return
        self._register(name, binding, registry)
        self._record_state(registry, name, "activated", tools=len(binding.tools))

    async def retry_pending(
        self,
        registry: PluginRegistry,
        *,
        push_event: Callable[[str, str], None],
    ) -> tuple[str, ...]:
        """Retente une fois chaque plugin en échec ; renvoie ceux encore en échec.

        Appelée par la tâche de fond de l'application (ADR-0005), jamais sur le
        chemin de traitement d'un Événement. Une ``PluginUnavailable`` garde le
        plugin en attente et laisse la boucle réessayer ; un défaut structurel
        (``create()`` qui lève autre chose, binding mal nommé, collision de
        Tools) est permanent : il est tracé ``failed`` au niveau ``error`` et
        retiré définitivement de la retente, comme au démarrage il aurait fait
        échouer le serveur.
        """
        for name in tuple(self._pending):
            pending = self._pending[name]
            try:
                binding = await self._bind(
                    name,
                    pending.create,
                    pending.config,
                    pending.secrets,
                    push_event,
                )
            except _CreateFailure as failure:
                self._record_state(
                    registry, name, "failed", reason=failure.reason
                )
                continue
            except PluginLoadError as exc:
                # Défaut du plugin découvert à la retente : le serveur tourne
                # déjà, donc il est tracé puis oublié au lieu de tuer le process.
                del self._pending[name]
                self._record_state(
                    registry,
                    name,
                    "failed",
                    reason=_redact(str(exc), pending.secrets.values()),
                    level="error",
                )
                continue
            del self._pending[name]
            try:
                self._register(name, binding, registry)
            except PluginLoadError as exc:
                self._record_state(
                    registry,
                    name,
                    "failed",
                    reason=_redact(str(exc), pending.secrets.values()),
                    level="error",
                )
                continue
            self._record_state(
                registry, name, "activated", tools=len(binding.tools)
            )
        return self.pending

    def _register(
        self, name: str, binding: PluginBinding, registry: PluginRegistry
    ) -> None:
        """Ajoute le binding au registre, sans jamais enregistrer à moitié.

        Les collisions sont vérifiées avant tout enregistrement pour qu'un
        plugin refusé ne laisse aucun de ses Tools derrière lui — un cas qui
        n'arrive qu'à la retente, où le registre vit déjà.
        """
        if binding.name != name:
            raise PluginLoadError(
                f"plugin {name!r}: create() returned a binding named "
                f"{binding.name!r}; it must match the plugin name"
            )
        for tool in binding.tools:
            owner = registry.tools.owner_of(tool.spec.name)
            if owner is not None:
                raise PluginLoadError(
                    f"plugin {name!r}: tool {tool.spec.name!r} is already "
                    f"registered by {owner!r}"
                )
        try:
            registry.add(binding)
        except ToolNameConflict as exc:
            # Défense en profondeur : deux Tools de même nom dans un seul
            # binding. Une collision reste un défaut fatal, jamais une retente.
            raise PluginLoadError(f"plugin {name!r}: {exc}") from exc


    def _read_manifest(self, name: str, plugin_dir: Path) -> PluginManifest:
        """Lit et valide strictement le manifeste d'un plugin activé."""
        data = self._read_manifest_data(name, plugin_dir)
        return self._parse_manifest(name, plugin_dir, data)

    def _read_manifest_data(self, name: str, plugin_dir: Path) -> dict:
        """Lit le JSON brut du manifeste ; un défaut d'I/O ou de forme est fatal."""
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
        return data

    def _parse_manifest(
        self, name: str, plugin_dir: Path, data: dict
    ) -> PluginManifest:
        """Valide un manifeste déjà lu ; partagé par le chargement et le balayage."""
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
        platform = fields["platform"]
        if platform not in KNOWN_PLATFORMS:
            # Une valeur inconnue est une faute de frappe, pas une plateforme
            # future : la refuser évite de croire un plugin chargé.
            raise PluginLoadError(
                f"plugin {name!r}: unknown platform {platform!r} "
                f"(known: {', '.join(KNOWN_PLATFORMS)})"
            )
        return PluginManifest(
            api_version=api_version,
            name=manifest_name,
            version=fields["version"],
            description=fields["description"],
            platform=platform,
            entrypoint=entrypoint,
            config=self._read_config_fields(name, data.get("config", [])),
            secrets=self._read_secret_fields(name, data.get("secrets", [])),
        )

    def _scan_installed(
        self, name: str, registry: PluginRegistry, raw_config: Mapping[str, object]
    ) -> None:
        """Classe un dossier non retenu en lisant son manifeste, sans l'importer.

        La plateforme est lue avant la validation complète : un manifeste qui
        annonce ``desktop`` reste ``unsupported`` même s'il lui manque un champ
        serveur. Tout autre défaut donne ``invalid`` avec une raison expurgée,
        sans interrompre le démarrage ; un manifeste sain donne ``inactive``.
        Les réglages observables le sont dans tous les cas : une valeur de
        configuration vient du fichier d'état même pour un dossier non retenu,
        et la présence des secrets est sondée sans jamais les stocker.
        """
        plugin_dir = self._directory / name
        try:
            data = self._read_manifest_data(name, plugin_dir)
        except PluginLoadError as exc:
            reason = _redact(str(exc), ())
            declaration = self._raw_declaration(
                name, plugin_dir, {}, invalid_reason=reason
            )
            self._declare(registry, name, declaration, raw_config)
            self._record_state(registry, name, "invalid", reason=reason)
            return
        platform = data.get("platform")
        if (
            isinstance(platform, str)
            and platform in KNOWN_PLATFORMS
            and platform != SERVER_PLATFORM
        ):
            declaration = self._declaration_or_raw(name, plugin_dir, data)
            self._declare(registry, name, declaration, raw_config)
            self._record_state(
                registry, name, "unsupported", platform=declaration.platform
            )
            return
        try:
            manifest = self._parse_manifest(name, plugin_dir, data)
        except PluginLoadError as exc:
            reason = _redact(str(exc), ())
            declaration = self._raw_declaration(
                name, plugin_dir, data, invalid_reason=reason
            )
            self._declare(registry, name, declaration, raw_config)
            self._record_state(registry, name, "invalid", reason=reason)
            return
        self._declare(registry, name, _declaration(manifest, plugin_dir), raw_config)
        self._record_state(registry, name, "inactive")

    def _declare(
        self,
        registry: PluginRegistry,
        name: str,
        declaration: PluginDeclaration,
        raw_config: Mapping[str, object],
    ) -> None:
        """Publie une Déclaration et les réglages qu'elle permet d'observer.

        Un manifeste invalide peut n'avoir qu'une Déclaration partielle : on ne
        sonde alors que les exigences qu'il permet encore de lire, jamais celles
        qu'on suppose.
        """
        registry.declare(name, declaration)
        registry.observe(
            name,
            self._observe(
                name, declaration.config, declaration.secrets, raw_config
            ),
        )

    def _declaration_or_raw(
        self, name: str, plugin_dir: Path, data: dict
    ) -> PluginDeclaration:
        """Déclaration complète quand le manifeste tient, identité seule sinon.

        Un plugin d'une autre plateforme n'est jamais chargé, donc un défaut qui
        ne concerne que le serveur (entrypoint absent) ne doit pas masquer son
        identité dans le registre. Les exigences encore lisibles sont conservées
        telles quelles, sans compléter ce que le manifeste ne permet pas de lire.
        """
        try:
            manifest = self._parse_manifest(name, plugin_dir, data)
        except PluginLoadError:
            return self._raw_declaration(name, plugin_dir, data)
        return _declaration(manifest, plugin_dir)

    def _raw_declaration(
        self,
        name: str,
        plugin_dir: Path,
        data: dict,
        *,
        invalid_reason: str | None = None,
    ) -> PluginDeclaration:
        """Identité lisible d'un manifeste invalide, sans inventer le reste.

        Le registre doit pouvoir nommer un dossier au manifeste cassé ; les
        champs qu'on ne sait pas valider restent vides plutôt que d'être
        devinés. Les listes de configuration et de secrets ne sont reprises que
        si leur forme tient encore : une entrée malformée fait perdre la liste
        entière, car une exigence à moitié lue vaudrait mieux comme inconnue.
        """

        def text(key: str) -> str:
            value = data.get(key)
            return value.strip() if isinstance(value, str) else ""

        try:
            config = self._read_config_fields(name, data.get("config", []))
        except PluginLoadError:
            config = ()
        try:
            secrets = self._read_secret_fields(name, data.get("secrets", []))
        except PluginLoadError:
            secrets = ()
        api_version = data.get("api_version")
        return PluginDeclaration(
            description=text("description"),
            version=text("version"),
            api_version=api_version if isinstance(api_version, int) else 0,
            platform=text("platform"),
            directory=str(plugin_dir),
            config=config,
            secrets=secrets,
            invalid_reason=invalid_reason,
        )

    def _observe(
        self,
        name: str,
        config_fields: tuple[ConfigField, ...],
        secret_fields: tuple[SecretField, ...],
        raw_config: Mapping[str, object],
    ) -> PluginObservation:
        """Observe les réglages sans jamais lever ni stocker un secret.

        Une clé laissée vide ou absente du fichier d'état vaut ``None`` : elle
        est au défaut du plugin. Une URL invalide est conservée telle quelle,
        pour que le diagnostic la nomme ; c'est la résolution stricte d'un
        plugin retenu qui refusera le démarrage. Seul ``MissingSecretError``
        vaut « absent » : toute autre exception du résolveur est un bogue, et
        remonte plutôt que de se déguiser en secret manquant.
        """
        config: list[ConfigValue] = []
        for field in config_fields:
            raw = raw_config.get(field.key)
            text = "" if raw is None else str(raw).strip()
            config.append(ConfigValue(key=field.key, value=text or None))
        secrets: list[SecretPresence] = []
        for field in secret_fields:
            variable = secret_variable(name, field.key)
            try:
                self._secrets.resolve(variable)
            except MissingSecretError:
                present = False
            else:
                present = True
            secrets.append(
                SecretPresence(key=field.key, variable=variable, present=present)
            )
        return PluginObservation(config=tuple(config), secrets=tuple(secrets))

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
            value_type = value_type.strip()
            if value_type not in _CONFIG_FIELD_TYPES:
                raise PluginLoadError(
                    f"plugin {name!r}: unknown type {value_type!r} for config key "
                    f"{key!r} (supported: {', '.join(_CONFIG_FIELD_TYPES)})"
                )
            fields.append(ConfigField(key=key, type=value_type, required=required))
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
        """Configuration non secrète déclarée par le manifeste, valeurs en texte.

        Chaque valeur non vide est validée selon le ``type`` déclaré ; une URL
        sans schéma ``http``/``https`` ou sans hôte est un défaut fatal. Un
        secret n'entre jamais ici (seuls les noms des secrets sont résolus
        ailleurs), donc aucun message ne peut en exposer un.
        """
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
            if field.type == "url" and not is_http_url(text):
                raise PluginLoadError(
                    f"plugin {manifest.name!r}: config key {field.key!r} must be "
                    "an http(s) URL with a host"
                )
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

    async def _bind(
        self,
        name: str,
        create: Callable[[PluginContext], object],
        config: Mapping[str, str],
        secrets: Mapping[str, str],
        push_event: Callable[[str, str], None],
    ) -> PluginBinding:
        """Fait rendre au plugin son binding, sans importer à nouveau.

        ``create`` peut être synchrone ou asynchrone : le serveur attend le
        résultat sans thread (une seule boucle, ADR-0005). Seule
        ``PluginUnavailable`` est une panne réparable : elle devient une
        ``_CreateFailure`` retentable, dont la raison est expurgée des valeurs
        de secrets. Toute autre exception est un défaut du plugin : elle lève
        ``PluginLoadError`` (fatal au démarrage, oublié à la retente), jamais
        une trace qui laisserait croire à une panne passagère. Une valeur de
        secret n'est jamais écrite dans la raison.
        """
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
        except PluginUnavailable as exc:
            raise _CreateFailure(
                _redact(f"{type(exc).__name__}: {exc}", secrets.values())
            ) from None
        except Exception as exc:
            # Bogue du plugin (TypeError, ValueError, import tardif…) : la
            # panne est permanente, donc fatale au démarrage.
            reason = _redact(f"{type(exc).__name__}: {exc}", secrets.values())
            raise PluginLoadError(
                f"plugin {name!r}: create() failed ({reason})"
            ) from None
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

    def _record_state(
        self,
        registry: PluginRegistry,
        plugin: str,
        state: str,
        *,
        level: str = "info",
        reason: str | None = None,
        **extra: object,
    ) -> None:
        """Met à jour l'état du plugin dans le registre, puis le trace.

        Le registre porte l'état lisible (glossaire), la raison expurgée et
        l'attente de retente ; la trace reste le flux d'observabilité, avec les
        mêmes payloads qu'avant. Une seule vérité pour la retente : elle se
        déduit de ``_pending``, et seule une trace ``failed`` l'annonce — un
        état réparé ou structurellement perdu n'a plus de tentative à venir.
        """
        retry = plugin in self._pending
        registry.mark_state(plugin, state, reason=reason, retry=retry)
        payload: dict[str, object] = {"plugin": plugin, "state": state, **extra}
        if reason is not None:
            payload["reason"] = reason
        if state == "failed":
            payload["retry"] = retry
        emit(
            self._trace_sink,
            _TRACE_SOURCE,
            "PluginState",
            payload=payload,
            level=level,
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


def _declaration(manifest: PluginManifest, plugin_dir: Path) -> PluginDeclaration:
    """Déclaration d'un manifeste validé, identité et exigences comprises."""
    return PluginDeclaration(
        description=manifest.description,
        version=manifest.version,
        api_version=manifest.api_version,
        platform=manifest.platform,
        directory=str(plugin_dir),
        config=manifest.config,
        secrets=manifest.secrets,
    )


def is_http_url(text: str) -> bool:
    """Vrai si ``text`` est une URL ``http``/``https`` avec un hôte non vide.

    Même règle que la résolution stricte d'un plugin retenu, exposée pour que
    l'instantané juge une URL déclarée sans réimporter la logique du chargeur.
    """
    try:
        parts = urlsplit(text)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def secret_variable(plugin: str, key: str) -> str:
    """Variable d'environnement d'un secret de plugin (ADR-0014).

    Forme ``ATHENA_PLUGIN_<PLUGIN>_<SECRET>``, en majuscules, tout caractère
    non alphanumérique remplacé par ``_``.
    """
    return f"{_SECRET_PREFIX}_{_sanitize(plugin)}_{_sanitize(key)}"


def _sanitize(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", text.upper())


def _redact(text: str, secrets: Iterable[str]) -> str:
    """Remplace toute valeur de secret connue par ``***`` dans un texte tracé."""
    for value in secrets:
        if value:
            text = text.replace(value, _REDACTED)
    return text
