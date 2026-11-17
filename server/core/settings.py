"""Vocabulaire pur des Réglages et des Secrets (ADR-0024, ADR-0025).

Le noyau nomme ce qui est réglable et ce qui est secret ; les adaptateurs
ouvrent le magasin, chiffrent et servent les routes. Aucun import d'entrée/sortie
ici : ce module reste utilisable par le noyau et par les garde-fous de pureté.

Les **Secrets** portent le nom de la variable d'environnement attendue : c'est
la même chaîne dans le produit, dans le magasin et dans la documentation.

La **table des champs** et le **moteur de résolution** sont la source unique de
la précédence : environnement du processus > magasin > ``.env`` > défauts. Le
``.env`` est présenté comme une valeur d'environnement ; le magasin est la seule
couche que l'interface peut écrire. Les chemins (bases, journaux, plugins) sont
en lecture seule : une faute de frappe ferait démarrer le serveur sur une base
vide ou rendrait la Console injoignable (ADR-0024).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence

# La clé Gemini est le seul Secret que l'Installation exige (ADR-0025). Les
# Secrets de plugin suivent la même convention ``ATHENA_PLUGIN_…`` (ADR-0014),
# mais ils ne sont connus qu'à la lecture d'un manifeste.
GEMINI_SECRET = "GEMINI_API_KEY"

# Secrets connus même quand ils ne sont pas encore enregistrés : la Console
# peut ainsi afficher la ligne « non configuré » sans inventer de nom.
KNOWN_SECRETS: tuple[str, ...] = (GEMINI_SECRET,)


class SettingSource(StrEnum):
    """Provenance d'une valeur effective, telle que l'interface la montre."""

    DEFAULT = "default"
    STORE = "store"
    ENV = "env"


@dataclass(frozen=True, slots=True)
class SettingField:
    """Un réglage déclaré : nom stable, variable d'environnement, défaut."""

    key: str
    env: str
    default: str
    kind: str = "string"
    #: Faux pour les chemins et les réglages dont une erreur est sans recours.
    editable: bool = True


@dataclass(frozen=True, slots=True)
class ResolvedSetting:
    """Valeur effective d'un champ et ce que l'interface doit en dire."""

    key: str
    env: str
    value: str
    default: str
    source: SettingSource
    editable: bool

    @property
    def read_only(self) -> bool:
        """Vrai si l'interface ne doit pas proposer d'écriture.

        Deux raisons distinctes, une seule réponse pour le formulaire : le
        champ est structurellement en lecture seule, ou l'environnement le
        masque. Dans les deux cas, enregistrer serait sans effet.
        """
        return not self.editable or self.source is SettingSource.ENV


# Table déclarative des réglages. Les défauts reprennent ceux du chargeur de
# configuration : c'est cette table que #174 exposera derrière l'API, et les
# deux doivent rester alignés (un test de comportement l'épingle).
SETTINGS: tuple[SettingField, ...] = (
    # Voix et modèles
    SettingField("voice", "GEMINI_VOICE", "Leda"),
    SettingField("live_model", "GEMINI_MODEL", "gemini-3.1-flash-live-preview"),
    SettingField("judge_model", "GEMINI_JUDGE_MODEL", "gemini-3.1-flash-lite"),
    SettingField(
        "consolidation_model", "ATHENA_CONSOLIDATION_MODEL", "gemini-3.1-flash-lite"
    ),
    # Mémoire et Consolidation
    SettingField("user_id", "ATHENA_USER_ID", "owner"),
    SettingField("short_term_window", "ATHENA_SHORT_TERM_WINDOW", "20", "int"),
    SettingField("recent_events_window", "ATHENA_RECENT_EVENTS_WINDOW", "20", "int"),
    SettingField("recall_k", "ATHENA_RECALL_K", "5", "int"),
    SettingField("recall_window_turns", "ATHENA_RECALL_WINDOW_TURNS", "3", "int"),
    SettingField(
        "recall_max_distance", "ATHENA_RECALL_MAX_DISTANCE", "0.6", "float"
    ),
    SettingField("dedup_threshold", "ATHENA_DEDUP_THRESHOLD", "0.15", "float"),
    SettingField(
        "journal_retention_days", "ATHENA_JOURNAL_RETENTION_DAYS", "3", "int"
    ),
    SettingField("consolidation_hours", "ATHENA_CONSOLIDATION_HOURS", "0,12"),
    # Surfaces
    SettingField("server_host", "WS_HOST", "0.0.0.0", editable=False),
    SettingField("server_port", "WS_PORT", "8765", "int", editable=False),
    SettingField("webhook_enabled", "ATHENA_WEBHOOK_ENABLED", "1", "bool"),
    SettingField("webhook_host", "ATHENA_WEBHOOK_HOST", "127.0.0.1"),
    SettingField("webhook_port", "ATHENA_WEBHOOK_PORT", "8766", "int"),
    SettingField("console_enabled", "ATHENA_CONSOLE_ENABLED", "1", "bool"),
    SettingField("console_host", "ATHENA_CONSOLE_HOST", "127.0.0.1"),
    SettingField("console_port", "ATHENA_CONSOLE_PORT", "8767", "int"),
    SettingField("console_trace_buffer", "ATHENA_CONSOLE_TRACE_BUFFER", "500", "int"),
    # Seuils d'authentification
    SettingField("ticket_days", "ATHENA_CONSOLE_TICKET_DAYS", "30", "int"),
    SettingField(
        "ticket_refresh_seconds",
        "ATHENA_CONSOLE_TICKET_REFRESH_SECONDS",
        "3600",
        "int",
    ),
    SettingField("password_min_length", "ATHENA_AUTH_PASSWORD_MIN_LENGTH", "12", "int"),
    SettingField("login_max_attempts", "ATHENA_AUTH_LOGIN_MAX_ATTEMPTS", "5", "int"),
    SettingField("login_block_minutes", "ATHENA_AUTH_LOGIN_BLOCK_MINUTES", "15", "int"),
    # Observabilité
    SettingField("log_level", "LOG_LEVEL", "INFO"),
    # Chemins en lecture seule : une valeur fausse est destructrice ou coupe
    # l'accès sans recours depuis l'interface.
    SettingField("memory_db", "ATHENA_MEMORY_DB", "athena_memory.db", editable=False),
    SettingField("auth_db", "ATHENA_AUTH_DB", "auth.db", editable=False),
    SettingField("settings_db", "ATHENA_SETTINGS_DB", "settings.db", editable=False),
    SettingField(
        "master_key_file", "ATHENA_MASTER_KEY_FILE", "master.key", editable=False
    ),
    SettingField("plugins_dir", "ATHENA_PLUGINS_DIR", "plugins", editable=False),
    SettingField("plugins_state", "ATHENA_PLUGINS_STATE", "plugins.json", editable=False),
    SettingField("embed_model", "ATHENA_EMBED_MODEL", "all-MiniLM-L6-v2", editable=False),
    SettingField("log_dir", "ATHENA_LOG_DIR", "logs", editable=False),
)

_FIELDS_BY_KEY = {field.key: field for field in SETTINGS}


def field_by_key(key: str) -> SettingField | None:
    """Champ déclaré par son nom stable, ou ``None``."""
    return _FIELDS_BY_KEY.get(key)


def resolve_setting(
    field: SettingField,
    *,
    process_env: Mapping[str, str],
    store: Mapping[str, str],
    dotenv: Mapping[str, str],
) -> ResolvedSetting:
    """Applique la précédence et rend la source de la valeur retenue.

    Une chaîne vide ne masque rien : elle vaut « non renseigné » à chaque
    couche, comme pour les Secrets. Le ``.env`` est rapporté comme ``env``.
    """
    value = process_env.get(field.env)
    if value:
        return _resolved(field, value, SettingSource.ENV)
    value = store.get(field.key)
    if value:
        return _resolved(field, value, SettingSource.STORE)
    value = dotenv.get(field.env)
    if value:
        return _resolved(field, value, SettingSource.ENV)
    return _resolved(field, field.default, SettingSource.DEFAULT)


def resolve_all(
    *,
    process_env: Mapping[str, str] | None = None,
    store: Mapping[str, str] | None = None,
    dotenv: Mapping[str, str] | None = None,
) -> Sequence[ResolvedSetting]:
    """Résout toute la table, dans l'ordre de déclaration."""
    process_env = process_env or {}
    store = store or {}
    dotenv = dotenv or {}
    return [
        resolve_setting(
            field, process_env=process_env, store=store, dotenv=dotenv
        )
        for field in SETTINGS
    ]


def _resolved(
    field: SettingField, value: str, source: SettingSource
) -> ResolvedSetting:
    return ResolvedSetting(
        key=field.key,
        env=field.env,
        value=value,
        default=field.default,
        source=source,
        editable=field.editable,
    )
