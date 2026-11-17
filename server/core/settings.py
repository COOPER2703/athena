"""Vocabulaire pur des Réglages et des Secrets (ADR-0025, ADR-0026).

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
vide ou rendrait la Console injoignable (ADR-0025).

Le ticket #174 ajoute ici la **validation** d'un objet complet, le calcul des
**changements en attente** (écart entre le magasin et la configuration chargée)
et la règle de **confirmation d'URL** quand un hôte ou un port change. Rien de
tout cela ne touche au disque : la route de la Console écrit, le noyau décide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field as dc_field
from enum import StrEnum
from typing import Mapping, Sequence
from urllib.parse import urlsplit

# La clé Gemini est le seul Secret que l'Installation exige (ADR-0026). Les
# Secrets de plugin suivent la même convention ``ATHENA_PLUGIN_…`` (ADR-0014),
# mais ils ne sont connus qu'à la lecture d'un manifeste.
GEMINI_SECRET = "GEMINI_API_KEY"

# Secrets connus même quand ils ne sont pas encore enregistrés : la Console
# peut ainsi afficher la ligne « non configuré » sans inventer de nom.
KNOWN_SECRETS: tuple[str, ...] = (GEMINI_SECRET,)

# Niveaux de log acceptés, dans l'ordre de gravité. La configuration les reprend
# tels quels : une seule liste, donc un seul refus possible.
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Commande montrée par le bandeau quand le redémarrage automatique est refusé.
# Elle correspond au déploiement canonique (conteneur ``athena``) ; un
# déploiement systemd la remplace par ``ATHENA_RESTART_COMMAND``.
DEFAULT_RESTART_COMMAND = "docker restart athena"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
# Une valeur de réglage reste courte : c'est un nom de modèle, d'hôte ou d'ID,
# jamais un texte libre. La borne protège le magasin d'une saisie aberrante.
_MAX_STRING_LENGTH = 256


class SettingSource(StrEnum):
    """Provenance d'une valeur effective, telle que l'interface la montre."""

    DEFAULT = "default"
    STORE = "store"
    ENV = "env"


@dataclass(frozen=True, slots=True)
class SettingField:
    """Un réglage déclaré : nom stable, variable d'environnement, défaut.

    ``kind`` borne le formulaire et la validation (``string``, ``int``,
    ``float``, ``bool``, ``hours``) ; ``minimum``, ``maximum`` et ``choices``
    sont exposés à l'interface pour qu'elle n'ait pas à redire les bornes.
    """

    key: str
    env: str
    default: str
    kind: str = "string"
    #: Faux pour les chemins et les réglages dont une erreur est sans recours.
    editable: bool = True
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


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


@dataclass(frozen=True, slots=True)
class SettingState:
    """Un champ tel que ``GET /api/settings`` le rend (ticket #174).

    ``value`` est la valeur **active**, celle que le processus a chargée ;
    ``pending_value`` est celle qui attend un redémarrage. Un champ en attente
    de retour au défaut a ``pending`` vrai et ``pending_value`` nul.
    """

    key: str
    env: str
    kind: str
    value: str
    default: str
    source: SettingSource
    editable: bool
    read_only: bool
    pending: bool
    pending_value: str | None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SettingsView:
    """Les couches figées au démarrage, telles que la racine les a chargées.

    ``loaded`` porte la valeur effective de chaque champ (issue de la
    ``AppConfig`` construite) ; ``applied_store`` ne retient que les champs du
    magasin qui ont réellement pris effet. La source se déduit de ces couches,
    jamais d'une relecture de l'environnement en cours de route.
    """

    loaded: Mapping[str, str]
    applied_store: Mapping[str, str] = dc_field(default_factory=dict)
    process_env: Mapping[str, str] = dc_field(default_factory=dict)
    dotenv: Mapping[str, str] = dc_field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Surface:
    """Une surface réseau dont l'adresse change au redémarrage."""

    name: str
    host_key: str
    port_key: str
    scheme: str


# Les trois Surfaces qui exigent une confirmation d'URL : la Console est en
# HTTP, le Webhook aussi, le transport parle le Protocole partagé en WebSocket.
SURFACES: tuple[Surface, ...] = (
    Surface("console", "console_host", "console_port", "http"),
    Surface("webhook", "webhook_host", "webhook_port", "http"),
    Surface("transport", "server_host", "server_port", "ws"),
)


@dataclass(frozen=True, slots=True)
class SettingsUpdate:
    """Ce qu'un ``PUT`` valide doit écrire dans le magasin.

    ``removals`` porte les champs revenus à leur défaut : la valeur stockée est
    effacée plutôt que figée, pour que la source redevienne ``default``.
    """

    writes: Mapping[str, str]
    removals: tuple[str, ...]


# Table déclarative des réglages. Les défauts reprennent ceux du chargeur de
# configuration : c'est cette table que #174 expose derrière l'API, et
# ``server/app/config.py`` la relit pour charger comme pour appliquer le
# magasin (un test de comportement épingle l'alignement des deux).
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
    SettingField("short_term_window", "ATHENA_SHORT_TERM_WINDOW", "20", "int", minimum=1),
    SettingField(
        "recent_events_window", "ATHENA_RECENT_EVENTS_WINDOW", "20", "int", minimum=1
    ),
    SettingField("recall_k", "ATHENA_RECALL_K", "5", "int", minimum=1),
    SettingField(
        "recall_window_turns", "ATHENA_RECALL_WINDOW_TURNS", "3", "int", minimum=1
    ),
    SettingField(
        "recall_max_distance",
        "ATHENA_RECALL_MAX_DISTANCE",
        "0.6",
        "float",
        minimum=0.0,
        maximum=2.0,
    ),
    SettingField(
        "dedup_threshold",
        "ATHENA_DEDUP_THRESHOLD",
        "0.15",
        "float",
        minimum=0.0,
        maximum=2.0,
    ),
    SettingField(
        "journal_retention_days",
        "ATHENA_JOURNAL_RETENTION_DAYS",
        "3",
        "int",
        minimum=1,
    ),
    SettingField(
        "consolidation_hours", "ATHENA_CONSOLIDATION_HOURS", "0,12", "hours"
    ),
    # Surfaces : un hôte ou un port ne change qu'avec la confirmation de la
    # nouvelle URL, parce qu'une faute coupe l'accès à la surface.
    SettingField("server_host", "WS_HOST", "0.0.0.0"),
    SettingField("server_port", "WS_PORT", "8765", "int", minimum=1, maximum=65535),
    SettingField("webhook_enabled", "ATHENA_WEBHOOK_ENABLED", "1", "bool"),
    SettingField("webhook_host", "ATHENA_WEBHOOK_HOST", "127.0.0.1"),
    SettingField(
        "webhook_port", "ATHENA_WEBHOOK_PORT", "8766", "int", minimum=1, maximum=65535
    ),
    SettingField("console_enabled", "ATHENA_CONSOLE_ENABLED", "1", "bool"),
    SettingField("console_host", "ATHENA_CONSOLE_HOST", "127.0.0.1"),
    SettingField(
        "console_port", "ATHENA_CONSOLE_PORT", "8767", "int", minimum=1, maximum=65535
    ),
    SettingField(
        "console_trace_buffer",
        "ATHENA_CONSOLE_TRACE_BUFFER",
        "500",
        "int",
        minimum=1,
    ),
    # Seuils d'authentification
    SettingField(
        "ticket_days", "ATHENA_CONSOLE_TICKET_DAYS", "30", "int", minimum=1
    ),
    SettingField(
        "ticket_refresh_seconds",
        "ATHENA_CONSOLE_TICKET_REFRESH_SECONDS",
        "3600",
        "int",
        minimum=0,
    ),
    SettingField(
        "password_min_length",
        "ATHENA_AUTH_PASSWORD_MIN_LENGTH",
        "12",
        "int",
        minimum=1,
    ),
    SettingField(
        "login_max_attempts", "ATHENA_AUTH_LOGIN_MAX_ATTEMPTS", "5", "int", minimum=1
    ),
    SettingField(
        "login_block_minutes", "ATHENA_AUTH_LOGIN_BLOCK_MINUTES", "15", "int", minimum=1
    ),
    # Observabilité
    SettingField("log_level", "LOG_LEVEL", "INFO", choices=LOG_LEVELS),
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

def describe_settings(
    view: SettingsView,
    store: Mapping[str, str],
) -> tuple[SettingState, ...]:
    """Rend chaque champ avec sa valeur active, sa source et son attente.

    La valeur vient de ``view.loaded`` — donc de la configuration **réellement
    chargée** —, jamais d'une resolve fraîche : le GET ne peut pas annoncer
    autre chose que ce que le processus utilise. La source, elle, se déduit des
    couches figées : l'environnement masque, puis le magasin appliqué, puis le
    ``.env`` (présenté comme ``env``), et le défaut sinon.
    """
    states: list[SettingState] = []
    for field in SETTINGS:
        value = view.loaded.get(field.key, field.default)
        source = _source(field, view)
        read_only = not field.editable or source is SettingSource.ENV
        pending = _pending(field, value, source, view, store)
        states.append(
            SettingState(
                key=field.key,
                env=field.env,
                kind=field.kind,
                value=value,
                default=field.default,
                source=source,
                editable=field.editable,
                read_only=read_only,
                pending=pending,
                pending_value=store.get(field.key) if pending else None,
                minimum=field.minimum,
                maximum=field.maximum,
                choices=field.choices,
            )
        )
    return tuple(states)


def normalize_value(field: SettingField, raw: object) -> tuple[str | None, str | None]:
    """Valide une valeur saisie et rend sa forme canonique.

    Rend ``(valeur, None)`` si la saisie est acceptable, ``(None, message)``
    sinon. Le message est une phrase française affichable telle quelle ; il ne
    contient jamais la valeur rejetée (une saisie peut être un secret recopié
    par erreur).
    """
    if not isinstance(raw, str):
        return None, "Une valeur texte est attendue."
    text = raw.strip()
    if field.kind == "bool":
        normalized = _normalize_bool(text)
        if normalized is None:
            return None, "Attendu : « 1 » ou « 0 »."
        return normalized, None
    if field.kind == "int":
        try:
            number = int(text)
        except ValueError:
            return None, "Un nombre entier est attendu."
        return _bounded(field, number, lambda value: str(value))
    if field.kind == "float":
        try:
            number = float(text)
        except ValueError:
            return None, "Un nombre décimal est attendu."
        if not math.isfinite(number):
            return None, "Un nombre décimal fini est attendu."
        return _bounded(field, number, lambda value: str(value))
    if field.kind == "hours":
        hours = _parse_hours(text)
        if hours is None:
            return None, (
                "Attendu : une liste d'heures entre 0 et 23, séparées par "
                "des virgules."
            )
        return ",".join(str(hour) for hour in hours), None
    if not text:
        return None, "Une valeur est attendue."
    if len(text) > _MAX_STRING_LENGTH:
        return None, "Cette valeur est trop longue."
    if field.choices:
        for choice in field.choices:
            if text == choice or text.upper() == choice.upper():
                return choice, None
        return None, "Valeur non reconnue : " + ", ".join(field.choices) + "."
    if field.key.endswith("_host"):
        message = _check_host(text)
        if message is not None:
            return None, message
    return text, None


def validate_settings_update(
    payload: object,
    *,
    states: Sequence[SettingState],
) -> tuple[SettingsUpdate | None, dict[str, str]]:
    """Valide un objet complet et rend l'écriture à appliquer, ou les erreurs.

    L'objet doit porter **tous** les champs éditables et non masqués. Un champ
    en lecture seule est toléré s'il est renvoyé inchangé (un formulaire peut
    renvoyer tout son état), refusé dès qu'il changerait. Rien n'est écrit tant
    que la validation n'a pas rendu zéro erreur.
    """
    if not isinstance(payload, Mapping):
        return None, {"values": "Un objet de réglages est attendu."}
    raw_values = payload.get("values")
    if not isinstance(raw_values, Mapping):
        return None, {"values": "Un objet « values » est attendu."}

    by_key = {state.key: state for state in states}
    errors: dict[str, str] = {}
    for raw_key in raw_values:
        key = str(raw_key)
        if key not in by_key:
            errors[key] = "Réglage inconnu."

    writes: dict[str, str] = {}
    removals: list[str] = []
    for state in states:
        if state.key not in raw_values:
            if state.editable and not state.read_only:
                errors[state.key] = "Ce champ est requis."
            continue
        field = _FIELDS_BY_KEY[state.key]
        normalized, message = normalize_value(field, raw_values[state.key])
        if message is not None:
            errors[state.key] = message
            continue
        if normalized is None:  # défensif : un message accompagne toujours None
            errors[state.key] = "Cette valeur n'est pas valide."
            continue
        if state.read_only:
            if not _same(field, normalized, state.value):
                if not state.editable:
                    errors[state.key] = "Ce réglage est en lecture seule."
                else:
                    errors[state.key] = (
                        "Ce champ est fixé par l'environnement ("
                        f"{state.env})."
                    )
            # Inchangé : accepté, mais jamais écrit.
            continue
        if not state.pending and normalized == state.value:
            # L'objet complet n'est pas un brouillon : renvoyer l'état courant
            # n'écrit rien, donc ne fige pas une valeur qui venait du ``.env``
            # ou d'un démarrage précédent.
            continue
        if normalized == state.default:
            removals.append(state.key)
        else:
            writes[state.key] = normalized

    if errors:
        return None, errors

    confirmation = _check_confirmation(
        payload, states=by_key, writes=writes, removals=removals
    )
    if confirmation is not None:
        return None, {"confirm_url": confirmation}

    return SettingsUpdate(writes=writes, removals=tuple(removals)), {}


def surface_url(surface: Surface, host: str, port: str) -> str:
    """URL affichable d'une surface, avec crochets pour une adresse IPv6."""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{surface.scheme}://{host}:{port}"


# ---------------------------------------------------------------------------
# Mécanique interne
# ---------------------------------------------------------------------------


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


def _source(field: SettingField, view: SettingsView) -> SettingSource:
    """Source d'une valeur, par le **même** moteur que ``resolve_setting``.

    ``describe_settings`` ne peut pas relire les valeurs (il rend celles que le
    processus a chargées), mais la provenance obéit à la précédence unique :
    environnement > magasin appliqué > ``.env`` > défaut.
    """
    return resolve_setting(
        field,
        process_env=view.process_env,
        store=view.applied_store,
        dotenv=view.dotenv,
    ).source


def _pending(
    field: SettingField,
    value: str,
    source: SettingSource,
    view: SettingsView,
    store: Mapping[str, str],
) -> bool:
    """Écart entre le magasin et la configuration chargée au démarrage.

    Un champ masqué par l'environnement ou structurellement en lecture seule
    n'attend rien : le magasin n'a aucun effet sur lui. Une valeur effacée
    après le démarrage attend aussi, puisqu'elle reviendra au défaut.
    """
    if source is SettingSource.ENV or not field.editable:
        return False
    stored = store.get(field.key)
    if stored is None:
        return field.key in view.applied_store
    return not _same(field, stored, value)


def _same(field: SettingField, raw: str, value: str) -> bool:
    """Compare une valeur brute du magasin à la valeur chargée.

    La comparaison passe par la forme canonique : un magasin qui porterait
    ``info`` pour ``log_level`` reste égal à la valeur chargée ``INFO``.
    """
    normalized, message = normalize_value(field, raw)
    if message is not None:
        return raw == value
    return normalized == value


def _check_confirmation(
    payload: Mapping[object, object],
    *,
    states: Mapping[str, SettingState],
    writes: Mapping[str, str],
    removals: Sequence[str],
) -> str | None:
    """Vérifie la confirmation des nouvelles URLs des surfaces déplacées.

    Rend ``None`` si tout est confirmé, ou le message d'erreur du champ
    ``confirm_url``. L'URL calculée est celle que le serveur servira après
    redémarrage : schéma, hôte et port, sans chemin.
    """
    required: list[str] = []
    changed_fields = {**writes}
    for key in removals:
        changed_fields[key] = _FIELDS_BY_KEY[key].default
    for surface in SURFACES:
        host_state = states[surface.host_key]
        port_state = states[surface.port_key]
        host = changed_fields.get(surface.host_key, host_state.value)
        port = changed_fields.get(surface.port_key, port_state.value)
        if host != host_state.value or port != port_state.value:
            required.append(surface_url(surface, host, port))
    if not required:
        return None

    confirmed = _confirmed_urls(payload.get("confirm_url"))
    expected = {_url_identity(url) for url in required}
    if confirmed is None or {_url_identity(url) for url in confirmed} != expected:
        return (
            "Confirmez la nouvelle adresse : "
            + ", ".join(required)
            + "."
        )
    return None


def _confirmed_urls(raw: object) -> list[str] | None:
    """Normalise ``confirm_url`` : une chaîne ou une liste de chaînes."""
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)) and all(
        isinstance(item, str) for item in raw
    ):
        return list(raw)
    return None


def _url_identity(url: str) -> object:
    """Identité comparable d'une URL : schéma, hôte et port, sans chemin.

    Une URL illisible est comparée telle quelle : elle ne peut alors égaler
    aucune URL calculée, ce qui produit le refus attendu.
    """
    parts = urlsplit(url.strip())
    try:
        port = parts.port
    except ValueError:
        return url.strip()
    if not parts.scheme or not parts.hostname or port is None:
        return url.strip()
    return (parts.scheme.lower(), parts.hostname.lower(), port)


def _normalize_bool(text: str) -> str | None:
    candidate = text.lower()
    if candidate in _TRUE_VALUES:
        return "1"
    if candidate in _FALSE_VALUES:
        return "0"
    return None


def _bounded(field: SettingField, number: float, render) -> tuple[str | None, str | None]:
    if field.minimum is not None and number < field.minimum:
        return None, f"La valeur minimale est {_number(field.minimum)}."
    if field.maximum is not None and number > field.maximum:
        return None, f"La valeur maximale est {_number(field.maximum)}."
    return render(number), None


def _number(value: float) -> str:
    """Rend une borne sans « .0 » inutile pour un entier."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _parse_hours(text: str) -> list[int] | None:
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if not parts:
        return None
    hours: list[int] = []
    for part in parts:
        try:
            hour = int(part)
        except ValueError:
            return None
        # 0..23 : une heure de Consolidation est une heure locale du jour.
        if not 0 <= hour <= 23:
            return None
        hours.append(hour)
    return hours


def _check_host(text: str) -> str | None:
    """Un hôte est un nom ou une adresse, jamais une URL déguisée."""
    if "/" in text or "://" in text:
        return "Indiquez un hôte seul, sans schéma ni chemin."
    if any(character.isspace() for character in text):
        return "Un hôte ne contient pas d'espace."
    return None
