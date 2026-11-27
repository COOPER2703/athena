from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.adapters.auth.store import (
    DEFAULT_LOGIN_BLOCK_SECONDS,
    DEFAULT_LOGIN_MAX_ATTEMPTS,
    DEFAULT_PASSWORD_MIN_LENGTH,
    DEFAULT_TICKET_REFRESH_SECONDS,
    DEFAULT_TICKET_TTL_SECONDS,
)
from server.adapters.memory.embedder import DEFAULT_EMBED_MODEL
from server.app.secrets import EnvSecretResolver, SecretResolver
from server.core.ports import MissingSecretError
from server.core.settings import (
    DEFAULT_RESTART_COMMAND,
    DEFAULT_TIMEZONE,
    LOG_LEVELS,
    SETTINGS,
)

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_GEMINI_VOICE = "Leda"
# Modèle de jugement : Flash-Lite, disponible sur l'échelle gratuite. Le défaut
# précédent (`gemini-3.8-flash`) épuisait le quota gratuit (20 requêtes/jour),
# répondait en ~30-50 s et renvoyait 503/429 : le juge échouait alors en faute
# fermée, donc plus aucune Session automatique ne se lançait.
DEFAULT_JUDGE_MODEL = "gemini-3.1-flash-lite"
DEFAULT_MEMORY_DB = "athena_memory.db"
# Base d'authentification **séparée** de la mémoire (spec #75) : Comptes,
# Tickets Console et Jetons d'accès se sauvegardent et se restaurent
# indépendamment. Comme la base mémoire, le défaut est relatif au répertoire
# de travail ; il est surchargeable par `ATHENA_AUTH_DB`.
DEFAULT_AUTH_DB = "auth.db"
# Les bornes de sécurité ont **une** source de vérité : le store d'auth, qui
# les applique. La configuration n'en est qu'une vue exprimée dans ses unités
# (jours, minutes), pour ne pas pouvoir diverger des valeurs réellement
# utilisées.
DEFAULT_CONSOLE_TICKET_DAYS = DEFAULT_TICKET_TTL_SECONDS // (24 * 60 * 60)
DEFAULT_CONSOLE_TICKET_REFRESH_SECONDS = DEFAULT_TICKET_REFRESH_SECONDS
DEFAULT_AUTH_PASSWORD_MIN_LENGTH = DEFAULT_PASSWORD_MIN_LENGTH
DEFAULT_AUTH_LOGIN_MAX_ATTEMPTS = DEFAULT_LOGIN_MAX_ATTEMPTS
DEFAULT_AUTH_LOGIN_BLOCK_MINUTES = DEFAULT_LOGIN_BLOCK_SECONDS // 60
# Modèle de Consolidation : Flash-Lite, disponible sur l'échelle gratuite. Le
# précédent défaut (`gemini-2.0-flash`) a été retiré et renvoyait 404.
DEFAULT_CONSOLIDATION_MODEL = "gemini-3.1-flash-lite"
DEFAULT_WEBHOOK_HOST = "127.0.0.1"
DEFAULT_WEBHOOK_PORT = 8766
# La Console web est une surface d'observation (ADR-0003) : port dédié,
# loopback par défaut, et authentification applicative depuis l'ADR-0017.
DEFAULT_CONSOLE_HOST = "127.0.0.1"
DEFAULT_CONSOLE_PORT = 8767
# Build de la SPA servi par la Console. Le répertoire est relatif au répertoire
# de travail du serveur ; absent, la Console explique quoi lancer sans empêcher
# l'API de répondre (le build n'est jamais committé).
DEFAULT_CONSOLE_STATIC_DIR = "web/dist"
# Nombre de Traces récentes rejouées à la connexion de la Console. Le tampon
# est en mémoire, depuis le démarrage du serveur ; il conserve tous les niveaux
# (le filtrage est une affaire de présentation).
DEFAULT_CONSOLE_TRACE_BUFFER = 500
DEFAULT_LOG_DIR = "logs"
# Un plugin est un dossier déposé (ADR-0014) ; l'état d'activation et la
# configuration non secrète vivent dans un fichier JSON séparé, à la racine du
# dépôt par défaut. Les deux chemins sont surchargeables pour les tests et les
# déploiements.
DEFAULT_PLUGINS_DIR = "plugins"
DEFAULT_PLUGINS_STATE = "plugins.json"
# Magasin de réglages et de Secrets (ADR-0025) : base dédiée, à côté de la
# mémoire et de l'auth, et clé maître dans un fichier séparé en ``0600``. Le
# chemin de la clé peut pointer vers un répertoire de credentials systemd.
DEFAULT_SETTINGS_DB = "settings.db"
DEFAULT_MASTER_KEY_FILE = "master.key"
# Planificateur (ADR-0028) : base dédiée, à côté de la mémoire, de l'auth et des
# réglages. Le fuseau est un nom IANA ; son défaut suit le processus (``TZ``,
# puis le fuseau système), aligné avec le Réglage ``timezone``.
DEFAULT_SCHEDULE_DB = "schedule.db"
# Redémarrage automatique après application des Réglages (ADR-0025). Désactivé
# par défaut : un serveur lancé à la main ne doit pas s'arrêter sans
# superviseur. Activé, le processus sort proprement et son superviseur le
# relance. La commande affichée sinon suit le déploiement : Docker est le seul
# artefact livré, un service systemd la remplace par ``ATHENA_RESTART_COMMAND``.
DEFAULT_RESTART_ON_APPLY = "0"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


def _coerce_bool(value: object, *, variable: str) -> bool:
    """Coerce une valeur d'environnement en booléen, avec un message explicite."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"Expected a boolean for {variable}, got {value!r}")


# Niveaux de log reconnus : la liste appartient au noyau (``LOG_LEVELS``), que
# la validation des Réglages applique aussi. Une seule liste, donc un seul
# ensemble de valeurs acceptées à l'écriture et au démarrage.
LogLevel = StrEnum("LogLevel", {level: level for level in LOG_LEVELS}, module=__name__)


class ServerConfig(BaseModel):
    """Paramètres réseau du serveur WebSocket."""

    model_config = ConfigDict(frozen=True)

    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1, le=65535)


class GeminiConfig(BaseModel):
    """Paramètres de connexion à Gemini Live.

    La clé est **facultative** (ADR-0026) : une instance neuve démarre sans
    elle, en mode Installation. Vide, elle ferme le transport, le Webhook et
    Gemini ; la Console et les Réglages restent joignables.
    """

    model_config = ConfigDict(frozen=True)

    api_key: str = ""
    model: str = DEFAULT_GEMINI_MODEL
    voice: str = DEFAULT_GEMINI_VOICE
    judge_model: str = Field(default=DEFAULT_JUDGE_MODEL, min_length=1)


class MemoryConfig(BaseModel):
    """Paramètres de la mémoire (Journal, mémoire long terme, embeddings).

    #26 définit le modèle figé complet pour que les tickets suivants
    (#27 Rappel, #29 Mémorisation explicite, #30 Consolidation) n'aient plus à
    le modifier.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(default="owner", min_length=1)
    db_path: str = Field(default=DEFAULT_MEMORY_DB, min_length=1)
    short_term_window: int = Field(default=20, ge=1)
    recent_events_window: int = Field(default=20, ge=1)
    recall_k: int = Field(default=5, ge=1)
    recall_window_turns: int = Field(default=3, ge=1)
    recall_max_distance: float = Field(default=0.6, ge=0.0, le=2.0)
    dedup_threshold: float = Field(default=0.15, ge=0.0, le=2.0)
    journal_retention_days: int = Field(default=3, ge=1)
    embed_model: str = Field(default=DEFAULT_EMBED_MODEL, min_length=1)
    consolidation_model: str = Field(
        default=DEFAULT_CONSOLIDATION_MODEL, min_length=1
    )
    consolidation_hours: tuple[int, ...] = (0, 12)

    @field_validator(
        "user_id", "db_path", "embed_model", "consolidation_model", mode="before"
    )
    @classmethod
    def _strip_non_empty(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("consolidation_hours", mode="before")
    @classmethod
    def _parse_hours(cls, value: object) -> object:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
            return [int(part) for part in parts]
        return value

    @field_validator("consolidation_hours")
    @classmethod
    def _check_hours(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("ATHENA_CONSOLIDATION_HOURS must list at least one hour")
        for hour in value:
            if not 0 <= hour <= 23:
                raise ValueError(
                    f"ATHENA_CONSOLIDATION_HOURS hours must be in [0, 23], got {hour}"
                )
        return value


class WebhookConfig(BaseModel):
    """Paramètres de l'adaptateur Webhook HTTP (ADR-0011, ADR-0017).

    Lié à ``127.0.0.1`` par défaut : la surface n'est ouverte qu'à la machine
    locale tant qu'on ne la déplace pas. Depuis l'ADR-0017, elle exige un
    **Jeton d'accès** de nature ``source`` en ``Authorization: Bearer`` ; la
    Source du Jeton fait autorité et une Source de corps contradictoire est
    refusée.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    host: str = Field(default=DEFAULT_WEBHOOK_HOST, min_length=1)
    port: int = Field(default=DEFAULT_WEBHOOK_PORT, ge=1, le=65535)

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled(cls, value: object) -> object:
        return _coerce_bool(value, variable="ATHENA_WEBHOOK_ENABLED")

    @field_validator("host", mode="before")
    @classmethod
    def _strip_host(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ConsoleConfig(BaseModel):
    """Paramètres de la Console web (ADR-0003, ADR-0016, ADR-0017).

    Surface d'observation servie sur son propre port, **liée à ``127.0.0.1``
    par défaut** : elle n'est joignable que depuis la machine du serveur tant
    qu'on ne la déplace pas. Elle est **activée par défaut**, comme le Webhook,
    et se désactive par ``ATHENA_CONSOLE_ENABLED=0``. Depuis l'ADR-0017, toute
    route d'API hors identité exige un Ticket Console ; ``auth=None`` au
    constructeur de l'adaptateur ne reste qu'un seam de test unitaire, jamais un
    mode de production.

    Défauts : ``enabled=True``, ``host="127.0.0.1"``, ``port=8767``,
    ``static_dir="web/dist"`` pour le build de la SPA et ``trace_buffer=500``
    pour l'historique rejoué à la connexion. Les cinq sont surchargeables par
    ``ATHENA_CONSOLE_ENABLED``, ``ATHENA_CONSOLE_HOST``, ``ATHENA_CONSOLE_PORT``,
    ``ATHENA_CONSOLE_STATIC_DIR`` et ``ATHENA_CONSOLE_TRACE_BUFFER``. Un
    ``static_dir`` absent n'est pas une erreur : la Console sert alors une page
    explicative et l'API répond normalement.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    host: str = Field(default=DEFAULT_CONSOLE_HOST, min_length=1)
    port: int = Field(default=DEFAULT_CONSOLE_PORT, ge=1, le=65535)
    static_dir: str = Field(default=DEFAULT_CONSOLE_STATIC_DIR, min_length=1)
    trace_buffer: int = Field(default=DEFAULT_CONSOLE_TRACE_BUFFER, ge=1)

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled(cls, value: object) -> object:
        return _coerce_bool(value, variable="ATHENA_CONSOLE_ENABLED")

    @field_validator("host", mode="before")
    @classmethod
    def _strip_host(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("static_dir", mode="before")
    @classmethod
    def _strip_static_dir(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AuthConfig(BaseModel):
    """Paramètres du module d'authentification (spec #75).

    La base d'auth est **distincte** de la mémoire ; elle est créée sans
    infrastructure de migration (``CREATE TABLE IF NOT EXISTS``). Les valeurs
    sont des bornes de sécurité, jamais des secrets : les Tickets Console et les
    Jetons d'accès sont générés à la demande.

    Défauts : ``auth.db``, Ticket de 30 jours glissants rafraîchi au plus une
    fois par heure, mot de passe d'au moins 12 caractères, blocage après
    cinq échecs pendant quinze minutes. Surchargeables par ``ATHENA_AUTH_DB``,
    ``ATHENA_CONSOLE_TICKET_DAYS``, ``ATHENA_CONSOLE_TICKET_REFRESH_SECONDS``,
    ``ATHENA_AUTH_PASSWORD_MIN_LENGTH``, ``ATHENA_AUTH_LOGIN_MAX_ATTEMPTS`` et
    ``ATHENA_AUTH_LOGIN_BLOCK_MINUTES``.
    """

    model_config = ConfigDict(frozen=True)

    db_path: str = Field(default=DEFAULT_AUTH_DB, min_length=1)
    ticket_days: int = Field(default=DEFAULT_CONSOLE_TICKET_DAYS, ge=1)
    ticket_refresh_seconds: int = Field(
        default=DEFAULT_CONSOLE_TICKET_REFRESH_SECONDS, ge=0
    )
    password_min_length: int = Field(
        default=DEFAULT_AUTH_PASSWORD_MIN_LENGTH, ge=1
    )
    login_max_attempts: int = Field(default=DEFAULT_AUTH_LOGIN_MAX_ATTEMPTS, ge=1)
    login_block_minutes: int = Field(default=DEFAULT_AUTH_LOGIN_BLOCK_MINUTES, ge=1)

    @field_validator("db_path", mode="before")
    @classmethod
    def _strip_db_path(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class PluginsConfig(BaseModel):
    """Emplacements des plugins serveur (ADR-0014).

    ``directory`` est le répertoire scanné au démarrage ; ``state_path`` le
    fichier d'état qui porte la liste d'activation explicite et la
    configuration non secrète. Un chemin absent ne fait pas échouer le
    démarrage : il signifie simplement « aucun plugin ».
    """

    model_config = ConfigDict(frozen=True)

    directory: str = Field(default=DEFAULT_PLUGINS_DIR, min_length=1)
    state_path: str = Field(default=DEFAULT_PLUGINS_STATE, min_length=1)

    @field_validator("directory", "state_path", mode="before")
    @classmethod
    def _strip_path(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SettingsConfig(BaseModel):
    """Emplacements du magasin de réglages et de la clé maître (ADR-0025).

    Les deux chemins sont en **lecture seule** dans l'interface : une faute de
    frappe ferait démarrer le serveur sur une base vide, ou rendrait les
    Secrets illisibles sans recours. ``master_key_file`` peut pointer vers le
    répertoire de credentials d'un service systemd (``systemd-creds``).
    """

    model_config = ConfigDict(frozen=True)

    db_path: str = Field(default=DEFAULT_SETTINGS_DB, min_length=1)
    master_key_file: str = Field(default=DEFAULT_MASTER_KEY_FILE, min_length=1)

    @field_validator("db_path", "master_key_file", mode="before")
    @classmethod
    def _strip_path(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ScheduleConfig(BaseModel):
    """Fuseau d'interprétation et base dédiée du Planificateur (ADR-0028).

    ``timezone`` est un nom IANA (``Europe/Paris``) : une règle quotidienne ou
    hebdomadaire s'interprète dans ce fuseau, tandis que les instants sont
    stockés en UTC. ``db_path`` est en lecture seule dans l'interface, comme
    les autres chemins de base.
    """

    model_config = ConfigDict(frozen=True)

    timezone: str = Field(default=DEFAULT_TIMEZONE, min_length=1)
    db_path: str = Field(default=DEFAULT_SCHEDULE_DB, min_length=1)

    @field_validator("timezone", mode="before")
    @classmethod
    def _check_timezone(cls, value: object) -> object:
        text = value.strip() if isinstance(value, str) else value
        try:
            ZoneInfo(str(text))
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError(
                "ATHENA_TIMEZONE must be an IANA timezone name"
            ) from None
        return text

    @field_validator("db_path", mode="before")
    @classmethod
    def _strip_path(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AppConfig(BaseModel):
    """Configuration validée au démarrage, injectée par constructeur."""

    model_config = ConfigDict(frozen=True)

    server: ServerConfig = Field(default_factory=ServerConfig)
    gemini: GeminiConfig
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    console: ConsoleConfig = Field(default_factory=ConsoleConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    settings: SettingsConfig = Field(default_factory=SettingsConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    log_level: LogLevel = LogLevel.INFO
    log_dir: str = Field(default=DEFAULT_LOG_DIR, min_length=1)
    debug_decisions: bool = False
    # Application des Réglages (ticket #174) : autorise la sortie propre du
    # processus, et nomme la commande montrée quand elle est refusée.
    restart_on_apply: bool = False
    restart_command: str = Field(default=DEFAULT_RESTART_COMMAND, min_length=1)

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("log_dir", "restart_command", mode="before")
    @classmethod
    def _strip_path(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("debug_decisions", mode="before")
    @classmethod
    def _debug_decisions(cls, value: object) -> object:
        return _coerce_bool(value, variable="ATHENA_DEBUG_DECISIONS")

    @field_validator("restart_on_apply", mode="before")
    @classmethod
    def _restart_on_apply(cls, value: object) -> object:
        return _coerce_bool(value, variable="ATHENA_RESTART_ON_APPLY")


# Où chaque champ de ``SETTINGS`` atterrit dans ``AppConfig``. La section vide
# désigne un champ de premier niveau. C'est cette table qui permet au chargeur,
# à la résolution et à l'application du magasin de ne jamais redire la liste des
# réglages ni le nom d'une variable d'environnement.
SETTING_TARGETS: dict[str, tuple[str, str]] = {
    "voice": ("gemini", "voice"),
    "live_model": ("gemini", "model"),
    "judge_model": ("gemini", "judge_model"),
    "consolidation_model": ("memory", "consolidation_model"),
    "user_id": ("memory", "user_id"),
    "short_term_window": ("memory", "short_term_window"),
    "recent_events_window": ("memory", "recent_events_window"),
    "recall_k": ("memory", "recall_k"),
    "recall_window_turns": ("memory", "recall_window_turns"),
    "recall_max_distance": ("memory", "recall_max_distance"),
    "dedup_threshold": ("memory", "dedup_threshold"),
    "journal_retention_days": ("memory", "journal_retention_days"),
    "consolidation_hours": ("memory", "consolidation_hours"),
    "server_host": ("server", "host"),
    "server_port": ("server", "port"),
    "webhook_enabled": ("webhook", "enabled"),
    "webhook_host": ("webhook", "host"),
    "webhook_port": ("webhook", "port"),
    "console_enabled": ("console", "enabled"),
    "console_host": ("console", "host"),
    "console_port": ("console", "port"),
    "console_trace_buffer": ("console", "trace_buffer"),
    "ticket_days": ("auth", "ticket_days"),
    "ticket_refresh_seconds": ("auth", "ticket_refresh_seconds"),
    "password_min_length": ("auth", "password_min_length"),
    "login_max_attempts": ("auth", "login_max_attempts"),
    "login_block_minutes": ("auth", "login_block_minutes"),
    "log_level": ("", "log_level"),
    "memory_db": ("memory", "db_path"),
    "auth_db": ("auth", "db_path"),
    "settings_db": ("settings", "db_path"),
    "master_key_file": ("settings", "master_key_file"),
    "timezone": ("schedule", "timezone"),
    "schedule_db": ("schedule", "db_path"),
    "plugins_dir": ("plugins", "directory"),
    "plugins_state": ("plugins", "state_path"),
    "embed_model": ("memory", "embed_model"),
    "log_dir": ("", "log_dir"),
}


def parse_dotenv(path: str | Path) -> dict[str, str]:
    """Lit un fichier ``.env`` simple en paires clé/valeur."""
    values: dict[str, str] = {}
    file_path = Path(path)
    if not file_path.is_file():
        return values
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def default_env_file() -> Path:
    """Fichier ``.env`` conventionnel : à la racine du répertoire courant."""
    return Path.cwd() / ".env"


def merged_environment(
    env: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
) -> dict[str, str]:
    """Vue fusionnée de la configuration : fichier ``.env`` puis environnement.

    L'environnement du processus prime sur le fichier, exactement comme pour
    ``load_config``. Sert aussi aux secrets de plugin : un jeton posé dans le
    ``.env`` doit être visible du plugin, comme ``GEMINI_API_KEY`` l'est du
    reste de la configuration.
    """
    file_values = parse_dotenv(env_file) if env_file is not None else {}
    process_env = os.environ if env is None else env
    return {**file_values, **process_env}


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
    secrets: SecretResolver | None = None,
) -> AppConfig:
    """Construit la configuration depuis l'environnement et un fichier ``.env``.

    L'environnement prime sur le fichier. Le secret Gemini est résolu via le
    ``SecretResolver`` injecté (ou un résolveur d'environnement par défaut) ;
    il est **facultatif** : sans clé, l'instance démarre en mode Installation
    (ADR-0026) et la racine de composition consultera le magasin de Secrets.

    Les champs viennent de la table ``SETTINGS`` : un réglage n'a qu'un nom,
    qu'une variable et qu'un défaut, ici comme dans l'API des Réglages. Les
    quelques champs hors table (clé Gemini, build statique, drapeaux internes)
    restent explicites.
    """
    merged = merged_environment(env, env_file=env_file)

    resolver: SecretResolver = (
        secrets if secrets is not None else EnvSecretResolver(merged)
    )
    try:
        api_key = resolver.resolve("GEMINI_API_KEY")
    except MissingSecretError:
        api_key = ""

    # Sections construites depuis la table : les valeurs restent des chaînes,
    # Pydantic applique les coercions et les bornes de chaque modèle.
    sections: dict[str, dict[str, object]] = {
        "server": {},
        "gemini": {"api_key": api_key},
        "memory": {},
        "webhook": {},
        "console": {
            "static_dir": merged.get(
                "ATHENA_CONSOLE_STATIC_DIR", DEFAULT_CONSOLE_STATIC_DIR
            )
        },
        "auth": {},
        "plugins": {},
        "settings": {},
        "schedule": {},
    }
    for field in SETTINGS:
        section, attribute = SETTING_TARGETS[field.key]
        value = merged.get(field.env, field.default)
        if section:
            sections[section][attribute] = value
        else:
            sections.setdefault("", {})[attribute] = value

    top_level = sections.pop("", {})
    return AppConfig(
        **sections,
        log_level=top_level.get("log_level", "INFO"),
        log_dir=top_level.get("log_dir", DEFAULT_LOG_DIR),
        debug_decisions=merged.get("ATHENA_DEBUG_DECISIONS", "0"),
        restart_on_apply=merged.get(
            "ATHENA_RESTART_ON_APPLY", DEFAULT_RESTART_ON_APPLY
        ),
        restart_command=merged.get("ATHENA_RESTART_COMMAND", DEFAULT_RESTART_COMMAND),
    )


def loaded_settings(config: AppConfig) -> dict[str, str]:
    """Valeurs de la configuration **réellement chargée**, par nom de champ.

    C'est la référence des « changements en attente » : le GET annonce ce que
    le processus utilise, jamais une re-résolution qui pourrait diverger.
    """
    return {
        field.key: _render_setting(config, SETTING_TARGETS[field.key])
        for field in SETTINGS
    }


def overlay_environment(
    config: AppConfig, env: Mapping[str, str]
) -> AppConfig:
    """Applique un environnement du processus à une configuration construite.

    ``load_config`` fait déjà ce travail. Cette variante sert aux compositions
    **programmatiques** (le harnais de test) qui doivent rester cohérentes avec
    la précédence : un environnement qui masque un réglage doit aussi être la
    valeur que le processus charge, sinon ``GET /api/settings`` annoncerait
    autre chose que le comportement réel.
    """
    if not env:
        return config
    data = config.model_dump()
    for field in SETTINGS:
        value = env.get(field.env)
        if not value:
            continue
        section, attribute = SETTING_TARGETS[field.key]
        holder = data[section] if section else data
        holder[attribute] = value
    return AppConfig.model_validate(data)


def config_with_stored_settings(
    config: AppConfig,
    *,
    store: Mapping[str, str],
    process_env: Mapping[str, str],
) -> tuple[AppConfig, dict[str, str]]:
    """Applique la couche « magasin » à une configuration chargée.

    La précédence de l'ADR-0025 vaut aussi au démarrage : environnement du
    processus > magasin > ``.env`` > défauts. La configuration reçue porte déjà
    l'environnement et le ``.env`` ; on ne superpose donc que les champs du
    magasin que l'environnement ne masque pas. Les chemins en lecture seule ne
    sont **jamais** repris du magasin, même édité à la main : ils ne peuvent
    pas déplacer une base ni couper l'accès.

    Rend la configuration effective et les champs du magasin qui ont
    réellement pris effet — la couche dont ``GET /api/settings`` tire la source.
    """
    applied: dict[str, str] = {}
    for field in SETTINGS:
        if not field.editable:
            continue
        if process_env.get(field.env):
            continue
        value = store.get(field.key)
        if value is not None:
            applied[field.key] = value
    if not applied:
        return config, {}

    data = config.model_dump()
    for key, value in applied.items():
        section, attribute = SETTING_TARGETS[key]
        holder = data[section] if section else data
        holder[attribute] = value
    # ``model_validate`` applique les bornes : une valeur stockée illisible fait
    # échouer le démarrage en nommant le champ, jamais démarrer sur autre chose.
    return AppConfig.model_validate(data), applied


def _render_setting(config: AppConfig, target: tuple[str, str]) -> str:
    section, attribute = target
    holder = getattr(config, section) if section else config
    value = getattr(holder, attribute)
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (tuple, list)):
        return ",".join(str(item) for item in value)
    return str(value)

