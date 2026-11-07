"""Plugin ``google-workspace`` : mails Gmail et agenda Google (ADR-0021).

Ce module ne connaît du serveur que le contrat public des plugins
(``plugin_api``) : il n'importe aucun interne. Il échange des refresh tokens
OAuth statiques contre des access tokens d'une heure, gardés en mémoire
seulement, pour plusieurs **Comptes Google** configurés par des libellés.

Au démarrage, ``create`` valide la configuration puis échange en parallèle le
refresh token de chaque compte. Si au moins un échange réussit, le plugin
s'active ; un compte en échec est tracé ``AccountUnavailable`` et retenté à
chaque appel de Tool le concernant. Si aucun ne réussit, ``PluginUnavailable``
laisse le chargeur démarrer dégradé et retenter en tâche de fond (ADR-0014).
Un appel de Tool, lui, ne lève jamais : une panne devient une phrase rendue au
modèle.

Le socle (comptes, jetons, HTTP, erreurs) est partagé : les Tools Gmail et
Calendar des tickets suivants n'ajoutent qu'un schéma et une opération. Ce
ticket apporte la recherche Gmail, la lecture de l'agenda, puis sa création,
sa modification et sa suppression confirmée.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping
from urllib.parse import quote

import httpx2

from plugin_api import (
    PluginBinding,
    PluginContext,
    PluginUnavailable,
    ToolBinding,
    ToolSpec,
)

# Valeurs sûres appliquées par le plugin quand la configuration manque : le
# chargeur ne sait pas porter de défaut (ADR-0014).
DEFAULT_API_BASE_URL = "https://www.googleapis.com"
DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Budgets HTTP : un seul essai, sauf le rejeu unique imposé après un 401.
TOKEN_TIMEOUT = 10.0
REQUEST_TIMEOUT = 10.0

# Un access token est rafraîchi soixante secondes avant son expiration réelle
# (ADR-0021). Constante lue à chaque opération, donc ajustable sur une copie.
TOKEN_REFRESH_MARGIN = 60.0
DEFAULT_EXPIRES_IN = 3600.0

# Bornes de ``gmail_search`` : dix messages par défaut, vingt-cinq au plus.
GMAIL_SEARCH_DEFAULT = 10
GMAIL_SEARCH_MAX = 25
GMAIL_SEARCH_MIN = 1

# Chemins Gmail. ``me`` désigne le compte porteur du jeton.
GMAIL_MESSAGES_PATH = "/gmail/v1/users/me/messages"

# En-têtes de métadonnées demandés à Gmail pour chaque message.
GMAIL_METADATA_HEADERS = ("From", "Subject", "Date")

# Fenêtre par défaut de ``calendar_list_events`` : maintenant, puis sept jours.
CALENDAR_DEFAULT_WINDOW_DAYS = 7

# Bornes de ``calendar_list_events`` : dix événements par défaut, cinquante au plus.
CALENDAR_MAX_RESULTS_DEFAULT = 10
CALENDAR_MAX_RESULTS_MAX = 50
CALENDAR_MAX_RESULTS_MIN = 1

# Chemin Calendar ; l'identifiant d'agenda y est encodé séparément.
CALENDAR_EVENTS_BASE_PATH = "/calendar/v3/calendars"

# -- Schémas des Tools --------------------------------------------------------

GMAIL_SEARCH_DESCRIPTION = (
    "Cherche des messages Gmail avec la syntaxe de recherche Gmail et rend "
    "leurs métadonnées."
)

GMAIL_SEARCH_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Requête de recherche Gmail (expéditeur, objet, date, mot-clé)."
            ),
        },
        "account": {
            "type": "string",
            "description": (
                "Clé du compte Google à utiliser ; le compte par défaut "
                "quand il est absent."
            ),
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": 25,
            "description": "Nombre de messages rendus, de 1 à 25. Défaut 10.",
        },
        "page_token": {
            "type": "string",
            "description": "Jeton de page rendu par l'appel précédent.",
        },
    },
    "required": ["query"],
}

CALENDAR_LIST_EVENTS_DESCRIPTION = (
    "Liste les événements d'agenda d'une période."
)

CALENDAR_LIST_EVENTS_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "account": {
            "type": "string",
            "description": (
                "Clé du compte Google à utiliser ; le compte par défaut "
                "quand il est absent."
            ),
        },
        "time_min": {
            "type": "string",
            "description": (
                "Début de la période, RFC 3339 avec décalage. Défaut : "
                "maintenant."
            ),
        },
        "time_max": {
            "type": "string",
            "description": (
                "Fin de la période, RFC 3339 avec décalage. Défaut : "
                "maintenant + sept jours."
            ),
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Nombre d'événements rendus, de 1 à 50. Défaut 10.",
        },
        "calendar_id": {
            "type": "string",
            "description": "Identifiant de l'agenda. Défaut : « primary ».",
        },
        "query": {
            "type": "string",
            "description": "Recherche plein texte dans les événements.",
        },
    },
}

CALENDAR_CREATE_EVENT_DESCRIPTION = "Crée un événement d'agenda."

CALENDAR_CREATE_EVENT_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Titre de l'événement.",
        },
        "start": {
            "type": "string",
            "description": (
                "Début, RFC 3339 avec décalage, ou AAAA-MM-JJ pour une "
                "journée entière."
            ),
        },
        "end": {
            "type": "string",
            "description": (
                "Fin, RFC 3339 avec décalage, ou AAAA-MM-JJ pour une journée "
                "entière (fin exclusive, un jour est ajouté)."
            ),
        },
        "all_day": {
            "type": "boolean",
            "description": "Journée entière. Défaut : faux.",
        },
        "description": {
            "type": "string",
            "description": "Description de l'événement.",
        },
        "location": {
            "type": "string",
            "description": "Lieu de l'événement.",
        },
        "attendees": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Adresses e-mail des participants.",
        },
        "calendar_id": {
            "type": "string",
            "description": "Identifiant de l'agenda. Défaut : « primary ».",
        },
        "notify_attendees": {
            "type": "boolean",
            "description": "Prévient les participants. Défaut : faux.",
        },
        "account": {
            "type": "string",
            "description": (
                "Clé du compte Google à utiliser ; le compte par défaut "
                "quand il est absent."
            ),
        },
    },
    "required": ["summary", "start", "end"],
}

CALENDAR_UPDATE_EVENT_DESCRIPTION = "Modifie un événement d'agenda existant."

CALENDAR_UPDATE_EVENT_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "event_id": {
            "type": "string",
            "description": (
                "Identifiant de l'événement rendu par `calendar_list_events`."
            ),
        },
        "calendar_id": {
            "type": "string",
            "description": "Identifiant de l'agenda. Défaut : « primary ».",
        },
        "summary": {
            "type": "string",
            "description": "Nouveau titre ; une chaîne vide l'efface.",
        },
        "start": {
            "type": "string",
            "description": (
                "Nouveau début, RFC 3339 avec décalage, ou AAAA-MM-JJ pour "
                "une journée entière."
            ),
        },
        "end": {
            "type": "string",
            "description": (
                "Nouvelle fin, RFC 3339 avec décalage, ou AAAA-MM-JJ pour une "
                "journée entière (fin exclusive, un jour est ajouté)."
            ),
        },
        "all_day": {
            "type": "boolean",
            "description": "Journée entière. Défaut : faux.",
        },
        "description": {
            "type": "string",
            "description": "Nouvelle description ; une chaîne vide l'efface.",
        },
        "location": {
            "type": "string",
            "description": "Nouveau lieu ; une chaîne vide l'efface.",
        },
        "attendees": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Participants ; la liste remplace la précédente.",
        },
        "notify_attendees": {
            "type": "boolean",
            "description": "Prévient les participants. Défaut : faux.",
        },
        "account": {
            "type": "string",
            "description": (
                "Clé du compte Google à utiliser ; le compte par défaut "
                "quand il est absent."
            ),
        },
    },
    "required": ["event_id"],
}

CALENDAR_DELETE_EVENT_DESCRIPTION = (
    "Supprime un événement d'agenda après confirmation."
)

CALENDAR_DELETE_EVENT_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "event_id": {
            "type": "string",
            "description": (
                "Identifiant de l'événement rendu par `calendar_list_events`."
            ),
        },
        "calendar_id": {
            "type": "string",
            "description": "Identifiant de l'agenda. Défaut : « primary ».",
        },
        "confirm": {
            "type": "boolean",
            "description": "Doit valoir vrai après confirmation explicite.",
        },
        "notify_attendees": {
            "type": "boolean",
            "description": "Prévient les participants. Défaut : faux.",
        },
        "account": {
            "type": "string",
            "description": (
                "Clé du compte Google à utiliser ; le compte par défaut "
                "quand il est absent."
            ),
        },
    },
    "required": ["event_id", "confirm"],
}

# -- Instruction de plugin ----------------------------------------------------

INSTRUCTION_TEMPLATE = (
    "Tu disposes des Tools Google Workspace pour les mails Gmail et l'agenda "
    "Google.\n"
    "\n"
    "COMPTES:\n"
    "- Comptes disponibles : {accounts}. Compte par défaut : {default}.\n"
    "- Utilise le compte que l'utilisateur nomme ; sinon le compte par défaut.\n"
    "- Si la demande peut viser plusieurs comptes et qu'aucun n'est nommé, "
    "demande lequel.\n"
    "- N'invente jamais un nom de compte.\n"
    "\n"
    "MAILS:\n"
    "- `gmail_search` d'abord pour trouver un message ; n'invente jamais un "
    "identifiant.\n"
    "- `gmail_read` pour lire un message ou un fil ; ne lis pas un fil entier "
    "si l'utilisateur ne demande qu'un message.\n"
    "- `gmail_send` et `gmail_reply` : relis à voix haute destinataires, objet "
    "et corps, puis attends un « oui » explicite avant d'envoyer. N'envoie "
    "jamais de ta propre initiative.\n"
    "- Le corps est du texte brut. Pas de HTML, pas de pièce jointe ; si un "
    "message en a, tu peux nommer les pièces jointes mais pas les lire.\n"
    "- Après un envoi, confirme brièvement.\n"
    "\n"
    "AGENDA:\n"
    "- `calendar_list_events` pour consulter ; `calendar_create_event`, "
    "`calendar_update_event` et `calendar_delete_event` pour écrire.\n"
    "- Les dates sont absolues, au format RFC 3339 avec décalage, ou "
    "`AAAA-MM-JJ` pour une journée entière.\n"
    "- Sans date du jour fournie dans le contexte, ne calcule pas « demain », "
    "« la semaine prochaine » : demande la date à l'utilisateur.\n"
    "- `calendar_delete_event` seulement après confirmation explicite, avec "
    "`confirm=true`.\n"
    "- `notify_attendees=true` seulement si l'utilisateur demande explicitement "
    "de prévenir les participants.\n"
    "- N'invente jamais un identifiant d'événement ; utilise celui rendu par "
    "`calendar_list_events`.\n"
    "\n"
    "ÉCHECS:\n"
    "- Si un Tool rend `status: \"error\"`, dis la phrase `message` telle "
    "quelle, sans réessayer à l'aveugle."
)

# -- Phrases françaises -------------------------------------------------------

_SEARCH_PREFIX = "La recherche Gmail n'a pas abouti : "

_UNREACHABLE_MESSAGE = (
    _SEARCH_PREFIX + "le service Gmail est injoignable. Réessaie dans un instant."
)
_TIMEOUT_MESSAGE = (
    _SEARCH_PREFIX
    + "le service Gmail n'a pas répondu à temps. Réessaie dans un instant."
)
_UNREADABLE_MESSAGE = (
    _SEARCH_PREFIX
    + "le service Gmail a renvoyé une réponse illisible. Réessaie dans un instant."
)
_REFUSED_MESSAGE = (
    _SEARCH_PREFIX + "l'accès au compte a été refusé. Réessaie dans un instant."
)
_FORBIDDEN_MESSAGE = _SEARCH_PREFIX + "l'accès à ce compte est refusé."
_RATE_MESSAGE = (
    _SEARCH_PREFIX
    + "trop de requêtes ont été envoyées. Réessaie dans un instant."
)
_VALIDATION_MESSAGE = (
    _SEARCH_PREFIX + "la requête a été refusée. Vérifie la syntaxe de recherche."
)
_NOT_FOUND_MESSAGE = (
    _SEARCH_PREFIX + "le message ou la boîte demandée est introuvable."
)

# Phrases propres à l'agenda : un échec de lecture ne nomme jamais Gmail.
_CALENDAR_PREFIX = "La lecture de l'agenda n'a pas abouti : "

_CALENDAR_UNREACHABLE_MESSAGE = (
    _CALENDAR_PREFIX
    + "le service Agenda est injoignable. Réessaie dans un instant."
)
_CALENDAR_TIMEOUT_MESSAGE = (
    _CALENDAR_PREFIX
    + "le service Agenda n'a pas répondu à temps. Réessaie dans un instant."
)
_CALENDAR_UNREADABLE_MESSAGE = (
    _CALENDAR_PREFIX
    + "le service Agenda a renvoyé une réponse illisible. "
    "Réessaie dans un instant."
)
_CALENDAR_REFUSED_MESSAGE = (
    _CALENDAR_PREFIX
    + "l'accès au compte a été refusé. Réessaie dans un instant."
)
_CALENDAR_FORBIDDEN_MESSAGE = _CALENDAR_PREFIX + "l'accès à cet agenda est refusé."
_CALENDAR_RATE_MESSAGE = (
    _CALENDAR_PREFIX
    + "trop de requêtes ont été envoyées. Réessaie dans un instant."
)
_CALENDAR_VALIDATION_MESSAGE = (
    _CALENDAR_PREFIX
    + "la période ou la requête a été refusée. Vérifie les dates demandées."
)
_CALENDAR_NOT_FOUND_MESSAGE = _CALENDAR_PREFIX + "l'agenda demandé est introuvable."

# Phrases propres aux écritures : chacune nomme l'action tentée.
_CALENDAR_CREATE_PREFIX = (
    "La création de l'événement d'agenda n'a pas abouti : "
)
_CALENDAR_UPDATE_PREFIX = (
    "La modification de l'événement d'agenda n'a pas abouti : "
)
_CALENDAR_DELETE_PREFIX = (
    "La suppression de l'événement d'agenda n'a pas abouti : "
)

_CALENDAR_CREATE_VALIDATION_MESSAGE = (
    _CALENDAR_CREATE_PREFIX
    + "le titre, le début et la fin sont requis."
)
_CALENDAR_UPDATE_VALIDATION_MESSAGE = (
    _CALENDAR_UPDATE_PREFIX + "l'identifiant de l'événement est requis."
)
_CALENDAR_DELETE_VALIDATION_MESSAGE = (
    _CALENDAR_DELETE_PREFIX + "l'identifiant de l'événement est requis."
)
_CALENDAR_DELETE_CONFIRM_MESSAGE = (
    _CALENDAR_DELETE_PREFIX
    + "une confirmation explicite est requise (confirm=true)."
)


class _TokenError(Exception):
    """Échec d'échange OAuth ; ``reason`` est sûre à tracer et à rendre."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _RequestFailure(Exception):
    """Panne HTTP d'un appel de Tool, déjà traduite en phrase française."""

    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class _AccountUnavailable(Exception):
    """Compte dont le refresh token ne peut pas être échangé à cet instant."""

    def __init__(self, account: str) -> None:
        self.account = account
        self.message = (
            f"Le compte « {account} » est indisponible : "
            "réautorise-le, puis réessaie."
        )
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class _Account:
    """Un Compte Google et son client OAuth ; aucun secret n'est jamais tracé."""

    key: str
    label: str
    client_id: str
    client_secret: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class _Settings:
    """Configuration validée du plugin, comptes compris."""

    accounts: tuple[_Account, ...]
    default_account: str
    api_base_url: str
    token_url: str

    def account(self, key: str) -> _Account | None:
        for account in self.accounts:
            if account.key == key:
                return account
        return None


@dataclass(frozen=True, slots=True)
class _AccessToken:
    """Access token en mémoire et l'instant de son expiration réelle."""

    value: str
    expires_at: float


class Trace:
    """Contrat de ``PluginContext.trace`` : genre, payload, niveau nommé."""

    def __call__(
        self,
        kind: str,
        payload: Mapping[str, object] | None = None,
        *,
        level: str = "info",
    ) -> None: ...


# Ces caches vivent avec le module, donc une retente de ``create()`` ne jette
# pas un jeton encore valide. Aucune persistance, aucun disque (ADR-0021).
_TOKENS: dict[str, _AccessToken] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


async def create(context: PluginContext) -> PluginBinding:
    """Valide les comptes, échange leurs refresh tokens, rend les Tools.

    Seule cette étape peut lever ``PluginUnavailable`` : si aucun compte ne
    peut être rafraîchi, la panne est réparable et le chargeur retente. Les
    défauts de configuration, eux, sont des erreurs ordinaires donc fatales.
    """
    settings = _settings(context)
    ready = await asyncio.gather(
        *(
            _prepare_account(settings, account, context.trace)
            for account in settings.accounts
        )
    )
    if not any(ready):
        raise PluginUnavailable(
            "google-workspace: aucun Compte Google n'a pu être rafraîchi"
        )
    return PluginBinding(
        name=context.name,
        tools=_tool_bindings(settings, context.trace),
        instruction=_instruction(settings),
    )


async def _prepare_account(
    settings: _Settings, account: _Account, trace: Trace
) -> bool:
    """Échange le refresh token d'un compte et trace son état de démarrage."""
    try:
        await _access_token(settings, account, trace, force=True)
    except asyncio.CancelledError:
        raise
    except _TokenError as exc:
        trace(
            "AccountUnavailable",
            {"account": account.key, "reason": exc.reason},
            level="warning",
        )
        return False
    except Exception as exc:  # défense : aucune valeur secrète dans la raison
        trace(
            "AccountUnavailable",
            {"account": account.key, "reason": type(exc).__name__},
            level="warning",
        )
        return False
    trace("AccountReady", {"account": account.key})
    return True


def _instruction(settings: _Settings) -> str:
    """Compose l'instruction en substituant les comptes et le défaut."""
    return INSTRUCTION_TEMPLATE.replace(
        "{accounts}", ", ".join(account.key for account in settings.accounts)
    ).replace("{default}", settings.default_account)


# -- Configuration ------------------------------------------------------------


def _settings(context: PluginContext) -> _Settings:
    """Valide les quatre listes parallèles, ou lève un défaut fatal explicite."""
    config = context.config
    secrets = context.secrets

    def text(key: str, default: str = "") -> str:
        return str(config.get(key, "")).strip() or default

    labels = _entries("accounts", text("accounts"))
    client_ids = _entries("client_id", str(secrets.get("client_id", "")))
    client_secrets = _entries(
        "client_secret", str(secrets.get("client_secret", ""))
    )
    refresh_tokens = _entries(
        "refresh_token", str(secrets.get("refresh_token", ""))
    )

    lengths = {
        "accounts": len(labels),
        "client_id": len(client_ids),
        "client_secret": len(client_secrets),
        "refresh_token": len(refresh_tokens),
    }
    if len(set(lengths.values())) != 1:
        rendered = ", ".join(f"{key}={value}" for key, value in lengths.items())
        raise RuntimeError(
            "configuration invalide : accounts, client_id, client_secret et "
            f"refresh_token doivent avoir la même longueur ({rendered})"
        )

    accounts: list[_Account] = []
    seen: dict[str, str] = {}
    for label, client_id, client_secret, refresh_token in zip(
        labels, client_ids, client_secrets, refresh_tokens
    ):
        key = _account_key(label)
        if not key:
            raise RuntimeError(
                "configuration invalide : le libellé de compte "
                f"{label!r} ne contient aucun caractère alphanumérique"
            )
        if key in seen:
            raise RuntimeError(
                "configuration invalide : les libellés "
                f"{seen[key]!r} et {label!r} produisent la clé {key!r} en double"
            )
        seen[key] = label
        accounts.append(
            _Account(
                key=key,
                label=label,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
            )
        )

    default_raw = text("default_account")
    if default_raw:
        default_account = _account_key(default_raw)
        if default_account not in seen:
            raise RuntimeError(
                "configuration invalide : default_account "
                f"{default_raw!r} ne correspond à aucun compte "
                f"(comptes connus : {', '.join(seen)})"
            )
    else:
        default_account = accounts[0].key

    return _Settings(
        accounts=tuple(accounts),
        default_account=default_account,
        api_base_url=text("api_base_url", DEFAULT_API_BASE_URL).rstrip("/"),
        token_url=text("token_url", DEFAULT_TOKEN_URL),
    )


def _entries(name: str, raw: str) -> list[str]:
    """Découpe une liste séparée par des virgules ; une entrée vide est fatale."""
    values = [value.strip() for value in raw.split(",")]
    for position, value in enumerate(values, start=1):
        if not value:
            raise RuntimeError(
                "configuration invalide : "
                f"{name} contient une entrée vide (position {position})"
            )
    return values


def _account_key(label: str) -> str:
    """Clé de compte : minuscules, accents retirés, non-alphanumériques en ``-``."""
    folded = unicodedata.normalize("NFKD", label)
    without_accents = "".join(
        character
        for character in folded
        if not unicodedata.combining(character)
    )
    replaced = "".join(
        character if character.isalnum() else "-"
        for character in without_accents.lower()
    )
    return re.sub(r"-+", "-", replaced).strip("-")


# -- Jetons OAuth -------------------------------------------------------------


async def _access_token(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    *,
    force: bool = False,
) -> str:
    """Rend un access token valide, rafraîchi 60 s avant expiration.

    Un verrou asyncio par compte sérialise les rafraîchissements : plusieurs
    Tools simultanés ne brûlent qu'un échange.
    """
    lock = _LOCKS.setdefault(account.key, asyncio.Lock())
    async with lock:
        now = asyncio.get_running_loop().time()
        cached = _TOKENS.get(account.key)
        if (
            not force
            and cached is not None
            and now < cached.expires_at - TOKEN_REFRESH_MARGIN
        ):
            return cached.value
        if force:
            _TOKENS.pop(account.key, None)
        return await _refresh(settings, account, trace)


async def _refresh(
    settings: _Settings, account: _Account, trace: Trace
) -> str:
    """``POST token_url`` en formulaire ; ne persiste jamais le jeton reçu."""
    form = {
        "grant_type": "refresh_token",
        "client_id": account.client_id,
        "client_secret": account.client_secret,
        "refresh_token": account.refresh_token,
    }
    try:
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(TOKEN_TIMEOUT)
        ) as client:
            response = await client.post(settings.token_url, data=form)
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException as exc:
        reason = "timeout"
        _trace_refresh_failed(trace, account, reason)
        raise _TokenError(reason) from exc
    except httpx2.RequestError as exc:
        reason = type(exc).__name__
        _trace_refresh_failed(trace, account, reason)
        raise _TokenError(reason) from exc
    except Exception as exc:  # défense : jamais de valeur secrète dans la raison
        reason = type(exc).__name__
        _trace_refresh_failed(trace, account, reason)
        raise _TokenError(reason) from exc

    if response.status_code != 200:
        reason = _oauth_error(response)
        _trace_refresh_failed(trace, account, reason)
        raise _TokenError(reason)

    try:
        body = response.json()
    except ValueError as exc:
        reason = "unreadable"
        _trace_refresh_failed(trace, account, reason)
        raise _TokenError(reason) from exc
    access = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(access, str) or not access.strip():
        reason = "unreadable"
        _trace_refresh_failed(trace, account, reason)
        raise _TokenError(reason)

    expires_in = _expires_in(
        body.get("expires_in") if isinstance(body, dict) else None
    )
    token = _AccessToken(
        value=access.strip(),
        expires_at=asyncio.get_running_loop().time() + expires_in,
    )
    _TOKENS[account.key] = token
    return token.value


# Codes d'erreur OAuth connus : seule cette liste peut devenir la raison
# tracée d'un échec de rafraîchissement. Un intermédiaire (proxy, réponse
# inattendue) qui glisserait un secret, une adresse ou un contenu dans
# ``error`` retombe donc sur le statut HTTP, jamais dans une Trace.
_OAUTH_ERRORS = frozenset(
    {
        "access_denied",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "server_error",
        "temporarily_unavailable",
        "unauthorized_client",
        "unsupported_grant_type",
    }
)


def _oauth_error(response: httpx2.Response) -> str:
    """Code d'erreur OAuth connu ou statut, jamais un texte arbitraire."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str) and error.strip() in _OAUTH_ERRORS:
            return error.strip()
    return f"HTTP {response.status_code}"


def _expires_in(value: object) -> float:
    """Durée de vie du jeton ; une valeur absente ou illisible vaut une heure."""
    if isinstance(value, bool):
        return DEFAULT_EXPIRES_IN
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return DEFAULT_EXPIRES_IN
    return DEFAULT_EXPIRES_IN


def _trace_refresh_failed(
    trace: Trace, account: _Account, reason: str
) -> None:
    """Trace un échange refusé sans jamais y placer un secret."""
    trace(
        "TokenRefreshFailed",
        {"account": account.key, "reason": reason},
        level="warning",
    )


# -- HTTP Gmail ---------------------------------------------------------------


async def _authorized_get(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    url: str,
    params: Mapping[str, object],
) -> httpx2.Response:
    """GET porteur du Bearer ; un 401 rafraîchit et rejoue une seule fois."""
    try:
        token = await _access_token(settings, account, trace)
    except _TokenError as exc:
        raise _AccountUnavailable(account.key) from exc

    response = await _get(url, token, params)
    if response.status_code != 401:
        return response

    try:
        token = await _access_token(settings, account, trace, force=True)
    except _TokenError as exc:
        raise _AccountUnavailable(account.key) from exc
    return await _get(url, token, params)


async def _get(
    url: str, token: str, params: Mapping[str, object]
) -> httpx2.Response:
    """Un GET, ou une ``_RequestFailure`` déjà traduite en français."""
    try:
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(REQUEST_TIMEOUT),
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            return await client.get(url, params=dict(params))
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException as exc:
        raise _RequestFailure(_TIMEOUT_MESSAGE, "timeout") from exc
    except httpx2.RequestError as exc:
        raise _RequestFailure(_UNREACHABLE_MESSAGE, type(exc).__name__) from exc
    except Exception as exc:  # défense : le handler ne lève jamais
        raise _RequestFailure(_UNREACHABLE_MESSAGE, type(exc).__name__) from exc


def _json_body(response: httpx2.Response) -> Mapping[str, Any]:
    """Corps JSON objet ; tout le reste est une réponse illisible."""
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise _RequestFailure(_UNREADABLE_MESSAGE, "unreadable") from exc
    if not isinstance(body, dict):
        raise _RequestFailure(_UNREADABLE_MESSAGE, "unreadable")
    return body


def _status_error(status: int) -> str:
    """Phrase française d'un statut Gmail, jamais une exception brute."""
    if status == 400:
        return _VALIDATION_MESSAGE
    if status == 401:
        return _REFUSED_MESSAGE
    if status == 403:
        return _FORBIDDEN_MESSAGE
    if status == 404:
        return _NOT_FOUND_MESSAGE
    if status in (429, 529):
        return _RATE_MESSAGE
    if status >= 500:
        return (
            _SEARCH_PREFIX
            + f"le service Gmail est indisponible (réponse {status})."
        )
    return _SEARCH_PREFIX + f"le service Gmail a renvoyé une erreur (réponse {status})."


# -- Tool ``gmail_search`` ----------------------------------------------------


def _tool_bindings(
    settings: _Settings, trace: Trace
) -> tuple[ToolBinding, ...]:
    """Les Tools du plugin ; le socle ajoute ici ceux des tickets suivants."""
    return (
        _gmail_search_tool(settings, trace),
        _calendar_list_events_tool(settings, trace),
        _calendar_create_event_tool(settings, trace),
        _calendar_update_event_tool(settings, trace),
        _calendar_delete_event_tool(settings, trace),
    )


def _gmail_search_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``gmail_search`` et son handler : un appel ne lève jamais."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _gmail_search(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="gmail_search",
            description=GMAIL_SEARCH_DESCRIPTION,
            parameters=GMAIL_SEARCH_PARAMETERS,
        ),
        handler=handle,
    )


async def _gmail_search(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Cherche des messages et rend leurs métadonnées, sans jamais lever."""
    tool = "gmail_search"
    started = asyncio.get_running_loop().time()

    account = _resolve_account(settings, arguments.get("account"))
    if account is None:
        raw = arguments.get("account")
        shown = raw.strip() if isinstance(raw, str) and raw.strip() else ""
        key = _account_key(shown) if shown else ""
        message = (
            f"Le compte « {shown} » est inconnu. "
            f"Comptes disponibles : {_account_names(settings)}."
        )
        return _fail(trace, tool, key, started, "invalid_account", message)

    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        message = _SEARCH_PREFIX + "la requête est vide."
        return _fail(trace, tool, account.key, started, "invalid_query", message)

    max_results = _bounded_int(
        arguments.get("max_results"),
        default=GMAIL_SEARCH_DEFAULT,
        minimum=GMAIL_SEARCH_MIN,
        maximum=GMAIL_SEARCH_MAX,
    )
    page_token = _optional_text(arguments.get("page_token"))

    try:
        messages, next_page_token = await _search_messages(
            settings,
            account,
            trace,
            query=query.strip(),
            max_results=max_results,
            page_token=page_token,
        )
    except asyncio.CancelledError:
        raise
    except _AccountUnavailable as exc:
        return _fail(trace, tool, account.key, started, "unavailable", exc.message)
    except _RequestFailure as exc:
        return _fail(trace, tool, account.key, started, exc.status, exc.message)
    except Exception as exc:  # défense : le handler ne lève jamais
        return _fail(
            trace,
            tool,
            account.key,
            started,
            type(exc).__name__,
            _UNREADABLE_MESSAGE,
        )

    payload: dict[str, object] = {
        "status": "ok",
        "account": account.key,
        "messages": messages,
    }
    if next_page_token:
        payload["next_page_token"] = next_page_token
    _trace_tool(trace, tool, account.key, started, "ok", len(messages))
    return payload


async def _search_messages(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    *,
    query: str,
    max_results: int,
    page_token: str | None,
) -> tuple[list[dict[str, object]], str | None]:
    """Liste les identifiants puis lit les métadonnées de chaque message."""
    params: dict[str, object] = {
        "q": query,
        "maxResults": max_results,
    }
    if page_token is not None:
        params["pageToken"] = page_token
    url = settings.api_base_url + GMAIL_MESSAGES_PATH
    response = await _authorized_get(settings, account, trace, url, params)
    if response.status_code >= 400:
        raise _RequestFailure(
            _status_error(response.status_code),
            f"HTTP {response.status_code}",
        )
    body = _json_body(response)

    identifiers: list[str] = []
    listed = body.get("messages")
    if isinstance(listed, list):
        for item in listed[:max_results]:
            identifier = _message_id(item)
            if identifier is not None:
                identifiers.append(identifier)

    messages = [
        await _message_metadata(settings, account, trace, identifier)
        for identifier in identifiers
    ]
    next_page_token = _optional_text(body.get("nextPageToken"))
    return messages, next_page_token


async def _message_metadata(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    identifier: str,
) -> dict[str, object]:
    """``GET /messages/{id}?format=metadata`` : jamais le corps ni les pièces."""
    params: dict[str, object] = {
        "format": "metadata",
        "metadataHeaders": list(GMAIL_METADATA_HEADERS),
    }
    url = settings.api_base_url + GMAIL_MESSAGES_PATH + "/" + identifier
    response = await _authorized_get(settings, account, trace, url, params)
    if response.status_code >= 400:
        raise _RequestFailure(
            _status_error(response.status_code),
            f"HTTP {response.status_code}",
        )
    body = _json_body(response)
    headers = _headers(body)
    return {
        "id": _text(body.get("id")) or identifier,
        "thread_id": _text(body.get("threadId")),
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": _text(body.get("snippet")),
    }


def _headers(message: Mapping[str, Any]) -> dict[str, str]:
    """En-têtes Gmail indexés en minuscules ; les noms connus sont rendus vides."""
    values: dict[str, str] = {}
    payload = message.get("payload")
    if isinstance(payload, dict):
        raw_headers = payload.get("headers")
        if isinstance(raw_headers, list):
            for header in raw_headers:
                if not isinstance(header, dict):
                    continue
                name = header.get("name")
                value = header.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    values[name.lower()] = value
    return values


def _message_id(item: object) -> str | None:
    """Identifiant d'un message listé, ou ``None`` si la forme est inattendue."""
    if not isinstance(item, dict):
        return None
    identifier = item.get("id")
    if isinstance(identifier, str) and identifier.strip():
        return identifier.strip()
    return None


# -- Tool ``calendar_list_events`` -------------------------------------------


def _calendar_list_events_tool(
    settings: _Settings, trace: Trace
) -> ToolBinding:
    """Déclare ``calendar_list_events`` et son handler : jamais d'exception."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _calendar_list_events(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="calendar_list_events",
            description=CALENDAR_LIST_EVENTS_DESCRIPTION,
            parameters=CALENDAR_LIST_EVENTS_PARAMETERS,
        ),
        handler=handle,
    )


async def _calendar_list_events(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Liste les événements d'une période, sans jamais lever."""
    tool = "calendar_list_events"
    started = asyncio.get_running_loop().time()

    account = _resolve_account(settings, arguments.get("account"))
    if account is None:
        raw = arguments.get("account")
        shown = raw.strip() if isinstance(raw, str) and raw.strip() else ""
        key = _account_key(shown) if shown else ""
        message = (
            f"Le compte « {shown} » est inconnu. "
            f"Comptes disponibles : {_account_names(settings)}."
        )
        return _fail(trace, tool, key, started, "invalid_account", message)

    calendar_id = _optional_text(arguments.get("calendar_id")) or "primary"
    time_min, time_max = _calendar_window(
        arguments.get("time_min"), arguments.get("time_max")
    )
    max_results = _bounded_int(
        arguments.get("max_results"),
        default=CALENDAR_MAX_RESULTS_DEFAULT,
        minimum=CALENDAR_MAX_RESULTS_MIN,
        maximum=CALENDAR_MAX_RESULTS_MAX,
    )
    page_token = _optional_text(arguments.get("page_token"))
    query = _optional_text(arguments.get("query"))

    try:
        events, next_page_token = await _list_calendar_events(
            settings,
            account,
            trace,
            calendar_id=calendar_id,
            time_min=time_min,
            time_max=time_max,
            max_results=max_results,
            page_token=page_token,
            query=query,
        )
    except asyncio.CancelledError:
        raise
    except _AccountUnavailable as exc:
        return _fail(trace, tool, account.key, started, "unavailable", exc.message)
    except _RequestFailure as exc:
        return _fail(trace, tool, account.key, started, exc.status, exc.message)
    except Exception as exc:  # défense : le handler ne lève jamais
        return _fail(
            trace,
            tool,
            account.key,
            started,
            type(exc).__name__,
            _CALENDAR_UNREADABLE_MESSAGE,
        )

    payload: dict[str, object] = {
        "status": "ok",
        "account": account.key,
        "calendar_id": calendar_id,
        "events": events,
    }
    if next_page_token:
        payload["next_page_token"] = next_page_token
    _trace_tool(trace, tool, account.key, started, "ok", len(events))
    return payload


def _calendar_window(
    raw_min: object, raw_max: object
) -> tuple[str, str]:
    """Période demandée, sinon maintenant puis maintenant + sept jours.

    Les valeurs rendues sont RFC 3339 avec décalage, calculées à l'appel.
    """
    now = datetime.now().astimezone()
    time_min = _optional_text(raw_min) or _rfc3339(now)
    time_max = _optional_text(raw_max) or _rfc3339(
        now + timedelta(days=CALENDAR_DEFAULT_WINDOW_DAYS)
    )
    return time_min, time_max


def _rfc3339(moment: datetime) -> str:
    """Instant RFC 3339 avec décalage, à la seconde."""
    return moment.isoformat(timespec="seconds")


async def _list_calendar_events(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    *,
    calendar_id: str,
    time_min: str,
    time_max: str,
    max_results: int,
    page_token: str | None,
    query: str | None,
) -> tuple[list[dict[str, object]], str | None]:
    """``GET /calendar/v3/calendars/{id}/events`` en événements dépliés."""
    params: dict[str, object] = {
        "timeMin": time_min,
        "timeMax": time_max,
        "maxResults": max_results,
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if page_token is not None:
        params["pageToken"] = page_token
    if query is not None:
        params["q"] = query

    url = (
        settings.api_base_url
        + CALENDAR_EVENTS_BASE_PATH
        + "/"
        + quote(calendar_id, safe="")
        + "/events"
    )
    response = await _calendar_authorized_get(
        settings, account, trace, url, params
    )
    if response.status_code >= 400:
        raise _RequestFailure(
            _calendar_status_error(response.status_code),
            f"HTTP {response.status_code}",
        )
    body = _calendar_json_body(response)

    events: list[dict[str, object]] = []
    listed = body.get("items")
    if isinstance(listed, list):
        for item in listed[:max_results]:
            event = _calendar_event(item)
            if event is not None:
                events.append(event)
    next_page_token = _optional_text(body.get("nextPageToken"))
    return events, next_page_token


def _calendar_event(item: object) -> dict[str, object] | None:
    """Événement rendu au modèle ; ``all_day`` suit ``start.date``."""
    if not isinstance(item, dict):
        return None
    start, all_day = _calendar_bound(item.get("start"))
    end, _ = _calendar_bound(item.get("end"))
    return {
        "id": _text(item.get("id")),
        "summary": _text(item.get("summary")),
        "start": start,
        "end": end,
        "all_day": all_day,
        "location": _text(item.get("location")),
        "attendees": _calendar_attendees(item.get("attendees")),
        "status": _text(item.get("status")),
        "html_link": _text(item.get("htmlLink")),
    }


def _calendar_bound(value: object) -> tuple[str, bool]:
    """Borne d'événement et son caractère journée entière."""
    if not isinstance(value, dict):
        return "", False
    date = value.get("date")
    if isinstance(date, str) and date.strip():
        return date.strip(), True
    moment = value.get("dateTime")
    if isinstance(moment, str):
        return moment, False
    return "", False


def _calendar_attendees(value: object) -> list[dict[str, str]]:
    """Participants réduits à leur courriel et à leur réponse."""
    if not isinstance(value, list):
        return []
    attendees: list[dict[str, str]] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        attendees.append(
            {
                "email": _text(entry.get("email")),
                "response_status": _text(entry.get("responseStatus")),
            }
        )
    return attendees


async def _calendar_authorized_get(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    url: str,
    params: Mapping[str, object],
) -> httpx2.Response:
    """GET porteur du Bearer ; un 401 rafraîchit et rejoue une seule fois."""
    try:
        token = await _access_token(settings, account, trace)
    except _TokenError as exc:
        raise _AccountUnavailable(account.key) from exc

    response = await _calendar_get(url, token, params)
    if response.status_code != 401:
        return response

    try:
        token = await _access_token(settings, account, trace, force=True)
    except _TokenError as exc:
        raise _AccountUnavailable(account.key) from exc
    return await _calendar_get(url, token, params)


async def _calendar_get(
    url: str, token: str, params: Mapping[str, object]
) -> httpx2.Response:
    """Un GET Calendar, ou une ``_RequestFailure`` déjà en français."""
    try:
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(REQUEST_TIMEOUT),
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            return await client.get(url, params=dict(params))
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException as exc:
        raise _RequestFailure(_CALENDAR_TIMEOUT_MESSAGE, "timeout") from exc
    except httpx2.RequestError as exc:
        raise _RequestFailure(
            _CALENDAR_UNREACHABLE_MESSAGE, type(exc).__name__
        ) from exc
    except Exception as exc:  # défense : le handler ne lève jamais
        raise _RequestFailure(
            _CALENDAR_UNREACHABLE_MESSAGE, type(exc).__name__
        ) from exc


def _calendar_json_body(response: httpx2.Response) -> Mapping[str, Any]:
    """Corps JSON objet ; tout le reste est une réponse illisible."""
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise _RequestFailure(
            _CALENDAR_UNREADABLE_MESSAGE, "unreadable"
        ) from exc
    if not isinstance(body, dict):
        raise _RequestFailure(_CALENDAR_UNREADABLE_MESSAGE, "unreadable")
    return body


def _calendar_status_error(status: int) -> str:
    """Phrase française d'un statut Calendar, jamais une exception brute."""
    if status == 400:
        return _CALENDAR_VALIDATION_MESSAGE
    if status == 401:
        return _CALENDAR_REFUSED_MESSAGE
    if status == 403:
        return _CALENDAR_FORBIDDEN_MESSAGE
    if status == 404:
        return _CALENDAR_NOT_FOUND_MESSAGE
    if status in (429, 529):
        return _CALENDAR_RATE_MESSAGE
    if status >= 500:
        return (
            _CALENDAR_PREFIX
            + f"le service Agenda est indisponible (réponse {status})."
        )
    return (
        _CALENDAR_PREFIX
        + f"le service Agenda a renvoyé une erreur (réponse {status})."
    )


# -- Tools ``calendar_create_event``, ``calendar_update_event`` et
# ``calendar_delete_event`` ---------------------------------------------------


def _calendar_create_event_tool(
    settings: _Settings, trace: Trace
) -> ToolBinding:
    """Déclare ``calendar_create_event`` : un appel ne lève jamais."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _calendar_create_event(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="calendar_create_event",
            description=CALENDAR_CREATE_EVENT_DESCRIPTION,
            parameters=CALENDAR_CREATE_EVENT_PARAMETERS,
        ),
        handler=handle,
    )


def _calendar_update_event_tool(
    settings: _Settings, trace: Trace
) -> ToolBinding:
    """Déclare ``calendar_update_event`` : un appel ne lève jamais."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _calendar_update_event(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="calendar_update_event",
            description=CALENDAR_UPDATE_EVENT_DESCRIPTION,
            parameters=CALENDAR_UPDATE_EVENT_PARAMETERS,
        ),
        handler=handle,
    )


def _calendar_delete_event_tool(
    settings: _Settings, trace: Trace
) -> ToolBinding:
    """Déclare ``calendar_delete_event`` : un appel ne lève jamais."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _calendar_delete_event(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="calendar_delete_event",
            description=CALENDAR_DELETE_EVENT_DESCRIPTION,
            parameters=CALENDAR_DELETE_EVENT_PARAMETERS,
        ),
        handler=handle,
    )


async def _calendar_create_event(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Crée un événement, sans jamais lever : une panne est une phrase."""
    tool = "calendar_create_event"
    started = asyncio.get_running_loop().time()

    account = _resolve_account(settings, arguments.get("account"))
    if account is None:
        raw = arguments.get("account")
        shown = raw.strip() if isinstance(raw, str) and raw.strip() else ""
        key = _account_key(shown) if shown else ""
        message = (
            f"Le compte « {shown} » est inconnu. "
            f"Comptes disponibles : {_account_names(settings)}."
        )
        return _fail(trace, tool, key, started, "invalid_account", message)

    summary = _optional_text(arguments.get("summary"))
    start = _optional_text(arguments.get("start"))
    end = _optional_text(arguments.get("end"))
    if summary is None or start is None or end is None:
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_event",
            _CALENDAR_CREATE_VALIDATION_MESSAGE,
        )

    calendar_id = _optional_text(arguments.get("calendar_id")) or "primary"
    try:
        event_id, html_link = await _create_calendar_event(
            settings,
            account,
            trace,
            calendar_id=calendar_id,
            arguments=arguments,
            summary=summary,
            start=start,
            end=end,
        )
    except asyncio.CancelledError:
        raise
    except _AccountUnavailable as exc:
        return _fail(trace, tool, account.key, started, "unavailable", exc.message)
    except _RequestFailure as exc:
        return _fail(trace, tool, account.key, started, exc.status, exc.message)
    except Exception as exc:  # défense : le handler ne lève jamais
        return _fail(
            trace,
            tool,
            account.key,
            started,
            type(exc).__name__,
            _CALENDAR_CREATE_PREFIX
            + "le service Agenda a renvoyé une réponse illisible. "
            "Réessaie dans un instant.",
        )

    _trace_tool(trace, tool, account.key, started, "ok")
    return {
        "status": "ok",
        "event_id": event_id,
        "html_link": html_link,
    }


async def _create_calendar_event(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    *,
    calendar_id: str,
    arguments: Mapping[str, object],
    summary: str,
    start: str,
    end: str,
) -> tuple[str, str]:
    """``POST /calendar/v3/calendars/{id}/events`` puis identifiant et lien."""
    params = {"sendUpdates": _send_updates(arguments)}
    body = _create_event_body(arguments, summary=summary, start=start, end=end)
    response = await _calendar_authorized_send(
        settings,
        account,
        trace,
        "POST",
        _calendar_events_url(settings, calendar_id),
        params=params,
        body=body,
        prefix=_CALENDAR_CREATE_PREFIX,
    )
    if response.status_code >= 400:
        raise _RequestFailure(
            _calendar_write_status_error(
                response.status_code, _CALENDAR_CREATE_PREFIX
            ),
            f"HTTP {response.status_code}",
        )
    rendered = _calendar_write_json_body(response, _CALENDAR_CREATE_PREFIX)
    return _text(rendered.get("id")), _text(rendered.get("htmlLink"))


async def _calendar_update_event(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Modifie partiellement un événement, sans jamais lever."""
    tool = "calendar_update_event"
    started = asyncio.get_running_loop().time()

    account = _resolve_account(settings, arguments.get("account"))
    if account is None:
        raw = arguments.get("account")
        shown = raw.strip() if isinstance(raw, str) and raw.strip() else ""
        key = _account_key(shown) if shown else ""
        message = (
            f"Le compte « {shown} » est inconnu. "
            f"Comptes disponibles : {_account_names(settings)}."
        )
        return _fail(trace, tool, key, started, "invalid_account", message)

    event_id = _optional_text(arguments.get("event_id"))
    if event_id is None:
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_event",
            _CALENDAR_UPDATE_VALIDATION_MESSAGE,
        )

    calendar_id = _optional_text(arguments.get("calendar_id")) or "primary"
    try:
        event_id, html_link = await _patch_calendar_event(
            settings,
            account,
            trace,
            calendar_id=calendar_id,
            event_id=event_id,
            arguments=arguments,
        )
    except asyncio.CancelledError:
        raise
    except _AccountUnavailable as exc:
        return _fail(trace, tool, account.key, started, "unavailable", exc.message)
    except _RequestFailure as exc:
        return _fail(trace, tool, account.key, started, exc.status, exc.message)
    except Exception as exc:  # défense : le handler ne lève jamais
        return _fail(
            trace,
            tool,
            account.key,
            started,
            type(exc).__name__,
            _CALENDAR_UPDATE_PREFIX
            + "le service Agenda a renvoyé une réponse illisible. "
            "Réessaie dans un instant.",
        )

    _trace_tool(trace, tool, account.key, started, "ok")
    return {
        "status": "ok",
        "event_id": event_id,
        "html_link": html_link,
    }


async def _patch_calendar_event(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    *,
    calendar_id: str,
    event_id: str,
    arguments: Mapping[str, object],
) -> tuple[str, str]:
    """``PATCH`` avec les seuls champs fournis, puis identifiant et lien."""
    params = {"sendUpdates": _send_updates(arguments)}
    body = _update_event_body(arguments)
    response = await _calendar_authorized_send(
        settings,
        account,
        trace,
        "PATCH",
        _calendar_events_url(settings, calendar_id, event_id),
        params=params,
        body=body,
        prefix=_CALENDAR_UPDATE_PREFIX,
    )
    if response.status_code >= 400:
        raise _RequestFailure(
            _calendar_write_status_error(
                response.status_code, _CALENDAR_UPDATE_PREFIX
            ),
            f"HTTP {response.status_code}",
        )
    rendered = _calendar_write_json_body(response, _CALENDAR_UPDATE_PREFIX)
    return _text(rendered.get("id")) or event_id, _text(
        rendered.get("htmlLink")
    )


async def _calendar_delete_event(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Supprime un événement confirmé ; un 404/410 est un succès idempotent."""
    tool = "calendar_delete_event"
    started = asyncio.get_running_loop().time()

    account = _resolve_account(settings, arguments.get("account"))
    if account is None:
        raw = arguments.get("account")
        shown = raw.strip() if isinstance(raw, str) and raw.strip() else ""
        key = _account_key(shown) if shown else ""
        message = (
            f"Le compte « {shown} » est inconnu. "
            f"Comptes disponibles : {_account_names(settings)}."
        )
        return _fail(trace, tool, key, started, "invalid_account", message)

    event_id = _optional_text(arguments.get("event_id"))
    if event_id is None:
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_event",
            _CALENDAR_DELETE_VALIDATION_MESSAGE,
        )
    if arguments.get("confirm") is not True:
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "confirmation_required",
            _CALENDAR_DELETE_CONFIRM_MESSAGE,
        )

    calendar_id = _optional_text(arguments.get("calendar_id")) or "primary"
    try:
        await _delete_calendar_event(
            settings,
            account,
            trace,
            calendar_id=calendar_id,
            event_id=event_id,
            arguments=arguments,
        )
    except asyncio.CancelledError:
        raise
    except _AccountUnavailable as exc:
        return _fail(trace, tool, account.key, started, "unavailable", exc.message)
    except _RequestFailure as exc:
        return _fail(trace, tool, account.key, started, exc.status, exc.message)
    except Exception as exc:  # défense : le handler ne lève jamais
        return _fail(
            trace,
            tool,
            account.key,
            started,
            type(exc).__name__,
            _CALENDAR_DELETE_PREFIX
            + "le service Agenda a renvoyé une réponse illisible. "
            "Réessaie dans un instant.",
        )

    _trace_tool(trace, tool, account.key, started, "ok")
    return {"status": "ok", "event_id": event_id}


async def _delete_calendar_event(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    *,
    calendar_id: str,
    event_id: str,
    arguments: Mapping[str, object],
) -> None:
    """``DELETE`` ; un événement déjà absent (404/410) est un succès."""
    response = await _calendar_authorized_send(
        settings,
        account,
        trace,
        "DELETE",
        _calendar_events_url(settings, calendar_id, event_id),
        params={"sendUpdates": _send_updates(arguments)},
        body=None,
        prefix=_CALENDAR_DELETE_PREFIX,
    )
    if response.status_code in (404, 410):
        return
    if response.status_code >= 400:
        raise _RequestFailure(
            _calendar_write_status_error(
                response.status_code, _CALENDAR_DELETE_PREFIX
            ),
            f"HTTP {response.status_code}",
        )


def _calendar_events_url(
    settings: _Settings, calendar_id: str, event_id: str | None = None
) -> str:
    """URL Calendar ; l'agenda et l'événement sont encodés séparément."""
    url = (
        settings.api_base_url
        + CALENDAR_EVENTS_BASE_PATH
        + "/"
        + quote(calendar_id, safe="")
        + "/events"
    )
    if event_id is not None:
        url += "/" + quote(event_id, safe="")
    return url


def _send_updates(arguments: Mapping[str, object]) -> str:
    """``all`` seulement si l'utilisateur demande de prévenir les participants."""
    return "all" if arguments.get("notify_attendees") is True else "none"


def _create_event_body(
    arguments: Mapping[str, object],
    *,
    summary: str,
    start: str,
    end: str,
) -> dict[str, object]:
    """Corps d'une création ; une journée entière porte une fin exclusive."""
    all_day = arguments.get("all_day") is True
    body: dict[str, object] = {
        "summary": summary,
        "start": _event_bound(start, all_day, _CALENDAR_CREATE_PREFIX),
        "end": _event_bound(
            end, all_day, _CALENDAR_CREATE_PREFIX, exclusive=True
        ),
    }
    description = arguments.get("description")
    if isinstance(description, str):
        body["description"] = description
    location = arguments.get("location")
    if isinstance(location, str):
        body["location"] = location
    attendees = _attendees_body(arguments.get("attendees"))
    if attendees is not None:
        body["attendees"] = attendees
    return body


def _update_event_body(arguments: Mapping[str, object]) -> dict[str, object]:
    """Corps d'un PATCH : seuls les champs fournis, une chaîne vide efface."""
    all_day = arguments.get("all_day") is True
    body: dict[str, object] = {}
    for key in ("summary", "description", "location"):
        value = arguments.get(key)
        if isinstance(value, str):
            body[key] = value
    start = _optional_text(arguments.get("start"))
    if start is not None:
        body["start"] = _event_bound(
            start, all_day, _CALENDAR_UPDATE_PREFIX
        )
    end = _optional_text(arguments.get("end"))
    if end is not None:
        body["end"] = _event_bound(
            end, all_day, _CALENDAR_UPDATE_PREFIX, exclusive=True
        )
    attendees = _attendees_body(arguments.get("attendees"))
    if attendees is not None:
        body["attendees"] = attendees
    return body


def _event_bound(
    value: str,
    all_day: bool,
    prefix: str,
    *,
    exclusive: bool = False,
) -> dict[str, str]:
    """Borne ``date`` ou ``dateTime`` ; une fin de journée entière est exclue."""
    if not all_day:
        return {"dateTime": value}
    try:
        moment = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise _RequestFailure(
            prefix
            + "la date de journée entière est invalide "
            "(AAAA-MM-JJ attendu).",
            "invalid_date",
        ) from exc
    if exclusive:
        moment += timedelta(days=1)
    return {"date": moment.isoformat()}


def _attendees_body(value: object) -> list[dict[str, str]] | None:
    """Participants au format Calendar ; une liste fournie remplace l'ancienne."""
    if not isinstance(value, list):
        return None
    attendees: list[dict[str, str]] = []
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            attendees.append({"email": entry.strip()})
    return attendees


def _calendar_write_json_body(
    response: httpx2.Response, prefix: str
) -> Mapping[str, Any]:
    """Corps JSON objet d'une écriture ; tout le reste est illisible."""
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise _RequestFailure(
            prefix
            + "le service Agenda a renvoyé une réponse illisible. "
            "Réessaie dans un instant.",
            "unreadable",
        ) from exc
    if not isinstance(body, dict):
        raise _RequestFailure(
            prefix
            + "le service Agenda a renvoyé une réponse illisible. "
            "Réessaie dans un instant.",
            "unreadable",
        )
    return body


def _calendar_write_status_error(status: int, prefix: str) -> str:
    """Phrase française d'un statut d'écriture Calendar."""
    if status == 400:
        return (
            prefix
            + "les données de l'événement ont été refusées. "
            "Vérifie les dates et le titre."
        )
    if status == 401:
        return prefix + "l'accès au compte a été refusé. Réessaie dans un instant."
    if status == 403:
        return prefix + "l'accès à cet agenda est refusé."
    if status == 404:
        return prefix + "l'événement ou l'agenda est introuvable."
    if status in (429, 529):
        return (
            prefix
            + "trop de requêtes ont été envoyées. Réessaie dans un instant."
        )
    if status >= 500:
        return prefix + f"le service Agenda est indisponible (réponse {status})."
    return prefix + f"le service Agenda a renvoyé une erreur (réponse {status})."


async def _calendar_authorized_send(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    method: str,
    url: str,
    *,
    params: Mapping[str, object],
    body: Mapping[str, object] | None,
    prefix: str,
) -> httpx2.Response:
    """Écriture portée par le Bearer ; un 401 rafraîchit et rejoue une fois."""
    try:
        token = await _access_token(settings, account, trace)
    except _TokenError as exc:
        raise _AccountUnavailable(account.key) from exc

    response = await _calendar_send(method, url, token, params, body, prefix)
    if response.status_code != 401:
        return response

    try:
        token = await _access_token(settings, account, trace, force=True)
    except _TokenError as exc:
        raise _AccountUnavailable(account.key) from exc
    return await _calendar_send(method, url, token, params, body, prefix)


async def _calendar_send(
    method: str,
    url: str,
    token: str,
    params: Mapping[str, object],
    body: Mapping[str, object] | None,
    prefix: str,
) -> httpx2.Response:
    """Une écriture Calendar, ou une ``_RequestFailure`` déjà en français."""
    request: dict[str, object] = {"params": dict(params)}
    if body is not None:
        request["json"] = dict(body)
    try:
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(REQUEST_TIMEOUT),
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            return await client.request(method, url, **request)
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException as exc:
        raise _RequestFailure(
            prefix
            + "le service Agenda n'a pas répondu à temps. "
            "Réessaie dans un instant.",
            "timeout",
        ) from exc
    except httpx2.RequestError as exc:
        raise _RequestFailure(
            prefix
            + "le service Agenda est injoignable. "
            "Réessaie dans un instant.",
            type(exc).__name__,
        ) from exc
    except Exception as exc:  # défense : le handler ne lève jamais
        raise _RequestFailure(
            prefix
            + "le service Agenda est injoignable. "
            "Réessaie dans un instant.",
            type(exc).__name__,
        ) from exc


def _resolve_account(settings: _Settings, raw: object) -> _Account | None:
    """Compte nommé (libellé ou clé), sinon le compte par défaut."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return settings.account(settings.default_account)
    if not isinstance(raw, str):
        return None
    return settings.account(_account_key(raw))


def _account_names(settings: _Settings) -> str:
    return ", ".join(account.key for account in settings.accounts)


def _bounded_int(
    value: object, *, default: int, minimum: int, maximum: int
) -> int:
    """Entier borné ; une valeur absente ou illisible retombe sur le défaut."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(float(value.strip()))
        except ValueError:
            return default
    else:
        return default
    return max(minimum, min(maximum, number))


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _trace_tool(
    trace: Trace,
    tool: str,
    account: str,
    started: float,
    status: str,
    result_count: int | None = None,
) -> None:
    """Trace un appel : outil, compte, durée, statut, nombre de résultats.

    Jamais la requête Gmail, un objet, une adresse ou un corps.
    """
    payload: dict[str, object] = {
        "tool": tool,
        "account": account,
        "duration_ms": round(
            (asyncio.get_running_loop().time() - started) * 1000, 1
        ),
        "status": status,
    }
    if result_count is not None:
        payload["result_count"] = result_count
    trace("ToolCalled", payload)


def _fail(
    trace: Trace,
    tool: str,
    account: str,
    started: float,
    status: str,
    message: str,
) -> Mapping[str, object]:
    """Trace l'échec puis rend la phrase française au modèle."""
    _trace_tool(trace, tool, account, started, "error")
    trace("ToolFailed", {"tool": tool, "account": account, "status": status})
    return {"status": "error", "message": message}
