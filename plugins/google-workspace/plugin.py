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
Calendar des tickets suivants n'ajoutent qu'un schéma et une opération.
"""

from __future__ import annotations

import asyncio
import base64
import email.policy
import re
import unicodedata
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from typing import Any, Iterable, Mapping

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

# En-têtes nécessaires pour répondre : fil, expéditeur, destinataires, objet.
GMAIL_REPLY_HEADERS = (
    "From",
    "To",
    "Cc",
    "Subject",
    "Message-ID",
    "References",
)

# Profil Gmail : sert uniquement à reconnaître sa propre adresse dans un
# « répondre à tous ».
GMAIL_PROFILE_PATH = "/gmail/v1/users/me/profile"

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

GMAIL_SEND_DESCRIPTION = "Envoie un mail en texte brut."

GMAIL_SEND_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Destinataires principaux, au moins un.",
        },
        "cc": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Destinataires en copie.",
        },
        "bcc": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Destinataires en copie cachée.",
        },
        "subject": {
            "type": "string",
            "description": "Objet du mail.",
        },
        "body": {
            "type": "string",
            "description": "Corps du mail, en texte brut.",
        },
        "account": {
            "type": "string",
            "description": (
                "Clé du compte Google à utiliser ; le compte par défaut "
                "quand il est absent."
            ),
        },
    },
    "required": ["to", "subject", "body"],
}

GMAIL_REPLY_DESCRIPTION = "Répond dans le fil d'un message Gmail."

GMAIL_REPLY_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "message_id": {
            "type": "string",
            "description": "Identifiant du message auquel répondre.",
        },
        "body": {
            "type": "string",
            "description": "Corps de la réponse, en texte brut.",
        },
        "reply_all": {
            "type": "boolean",
            "description": (
                "Répondre aussi aux autres destinataires du message d'origine."
            ),
        },
        "account": {
            "type": "string",
            "description": (
                "Clé du compte Google à utiliser ; le compte par défaut "
                "quand il est absent."
            ),
        },
    },
    "required": ["message_id", "body"],
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

# Phrases propres à l'envoi et à la réponse : le préfixe change, pas le reste.
_SEND_PREFIX = "L'envoi du mail n'a pas abouti : "
_REPLY_PREFIX = "La réponse au mail n'a pas abouti : "

_DELIVERY_UNREACHABLE = (
    "le service Gmail est injoignable. Réessaie dans un instant."
)
_DELIVERY_TIMEOUT = (
    "le service Gmail n'a pas répondu à temps. Réessaie dans un instant."
)
_DELIVERY_UNREADABLE = (
    "le service Gmail a renvoyé une réponse illisible. Réessaie dans un instant."
)
_DELIVERY_REFUSED = "l'accès au compte a été refusé. Réessaie dans un instant."
_DELIVERY_FORBIDDEN = "l'accès à ce compte est refusé."
_DELIVERY_RATE = "trop de requêtes ont été envoyées. Réessaie dans un instant."
_DELIVERY_VALIDATION = "la requête a été refusée."
_DELIVERY_NOT_FOUND = "le message demandé est introuvable."


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


def _oauth_error(response: httpx2.Response) -> str:
    """Code d'erreur OAuth (``invalid_grant``…) ou statut, jamais un secret."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str) and error.strip():
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
        _gmail_send_tool(settings, trace),
        _gmail_reply_tool(settings, trace),
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


# -- Tools ``gmail_send`` et ``gmail_reply`` ----------------------------------


def _gmail_send_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``gmail_send`` et son handler : un appel ne lève jamais."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _gmail_send(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="gmail_send",
            description=GMAIL_SEND_DESCRIPTION,
            parameters=GMAIL_SEND_PARAMETERS,
        ),
        handler=handle,
    )


async def _gmail_send(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Compose un MIME texte UTF-8 et l'envoie, sans jamais lever."""
    tool = "gmail_send"
    started = asyncio.get_running_loop().time()

    account = _resolve_account(settings, arguments.get("account"))
    if account is None:
        raw = arguments.get("account")
        shown = raw.strip() if isinstance(raw, str) and raw.strip() else ""
        key = _account_key(shown) if shown else ""
        return _fail(
            trace,
            tool,
            key,
            started,
            "invalid_account",
            _unknown_account_message(settings, shown),
        )

    to = _addresses(arguments.get("to"))
    cc = _addresses(arguments.get("cc"))
    bcc = _addresses(arguments.get("bcc"))
    subject = arguments.get("subject")
    body = arguments.get("body")

    if not to:
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_to",
            _SEND_PREFIX + "le destinataire est absent ou invalide.",
        )
    if cc is None or bcc is None:
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_recipient",
            _SEND_PREFIX + "un destinataire en copie est invalide.",
        )
    if not isinstance(subject, str):
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_subject",
            _SEND_PREFIX + "l'objet est absent.",
        )
    if not isinstance(body, str) or not body.strip():
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_body",
            _SEND_PREFIX + "le corps est vide.",
        )

    try:
        raw = _compose_raw_message(
            to=to,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
        )
        sent = await _send_raw_message(
            settings,
            account,
            trace,
            raw=raw,
            thread_id=None,
            prefix=_SEND_PREFIX,
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
            _SEND_PREFIX + _DELIVERY_UNREADABLE,
        )

    payload: dict[str, object] = {
        "status": "ok",
        "message_id": _text(sent.get("id")),
        "thread_id": _text(sent.get("threadId")),
    }
    _trace_tool(trace, tool, account.key, started, "ok")
    return payload


def _gmail_reply_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``gmail_reply`` et son handler : un appel ne lève jamais."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _gmail_reply(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="gmail_reply",
            description=GMAIL_REPLY_DESCRIPTION,
            parameters=GMAIL_REPLY_PARAMETERS,
        ),
        handler=handle,
    )


async def _gmail_reply(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Répond dans le fil d'un message, sans jamais lever."""
    tool = "gmail_reply"
    started = asyncio.get_running_loop().time()

    account = _resolve_account(settings, arguments.get("account"))
    if account is None:
        raw = arguments.get("account")
        shown = raw.strip() if isinstance(raw, str) and raw.strip() else ""
        key = _account_key(shown) if shown else ""
        return _fail(
            trace,
            tool,
            key,
            started,
            "invalid_account",
            _unknown_account_message(settings, shown),
        )

    message_id = _optional_text(arguments.get("message_id"))
    body = arguments.get("body")
    if message_id is None:
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_message_id",
            _REPLY_PREFIX + "l'identifiant du message d'origine est absent.",
        )
    if not isinstance(body, str) or not body.strip():
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_body",
            _REPLY_PREFIX + "le corps est vide.",
        )
    reply_all = arguments.get("reply_all") is True

    try:
        original = await _reply_source(
            settings, account, trace, message_id
        )
        headers = _headers(original)
        thread_id = _text(original.get("threadId"))
        in_reply_to, references = _reply_references(headers)

        self_address = (
            await _profile_address(settings, account, trace)
            if reply_all
            else ""
        )
        to, cc = _reply_recipients(
            headers, self_address=self_address, reply_all=reply_all
        )
        if not to:
            raise _RequestFailure(
                _REPLY_PREFIX
                + "le destinataire du message d'origine est introuvable.",
                "unreadable",
            )

        raw = _compose_raw_message(
            to=to,
            cc=cc,
            subject=_reply_subject(headers.get("subject", "")),
            body=body,
            in_reply_to=in_reply_to,
            references=references,
        )
        sent = await _send_raw_message(
            settings,
            account,
            trace,
            raw=raw,
            thread_id=thread_id or None,
            prefix=_REPLY_PREFIX,
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
            _REPLY_PREFIX + _DELIVERY_UNREADABLE,
        )

    response_thread = _text(sent.get("threadId")) or thread_id
    payload: dict[str, object] = {
        "status": "ok",
        "message_id": _text(sent.get("id")),
        "thread_id": response_thread,
    }
    _trace_tool(trace, tool, account.key, started, "ok")
    return payload


async def _reply_source(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    message_id: str,
) -> Mapping[str, Any]:
    """Métadonnées du message d'origine, jamais son corps ni ses pièces."""
    params: dict[str, object] = {
        "format": "metadata",
        "metadataHeaders": list(GMAIL_REPLY_HEADERS),
    }
    url = settings.api_base_url + GMAIL_MESSAGES_PATH + "/" + message_id
    response = await _authorized_delivery(
        settings,
        account,
        trace,
        "GET",
        url,
        prefix=_REPLY_PREFIX,
        params=params,
    )
    if response.status_code >= 400:
        raise _RequestFailure(
            _delivery_status_error(_REPLY_PREFIX, response.status_code),
            f"HTTP {response.status_code}",
        )
    return _delivery_json_body(response, _REPLY_PREFIX)


async def _profile_address(
    settings: _Settings,
    account: _Account,
    trace: Trace,
) -> str:
    """Adresse du compte, pour s'exclure soi-même d'un « répondre à tous »."""
    url = settings.api_base_url + GMAIL_PROFILE_PATH
    response = await _authorized_delivery(
        settings, account, trace, "GET", url, prefix=_REPLY_PREFIX
    )
    if response.status_code >= 400:
        raise _RequestFailure(
            _delivery_status_error(_REPLY_PREFIX, response.status_code),
            f"HTTP {response.status_code}",
        )
    body = _delivery_json_body(response, _REPLY_PREFIX)
    address = _optional_text(body.get("emailAddress"))
    if address is None:
        raise _RequestFailure(
            _REPLY_PREFIX + "le profil Gmail est introuvable.", "unreadable"
        )
    return address


_REPLY_SUBJECT = re.compile(r"^\s*re\s*:", re.IGNORECASE)


def _reply_subject(subject: str) -> str:
    """Objet ``Re:`` sans empiler les préfixes déjà présents."""
    if _REPLY_SUBJECT.match(subject):
        return subject
    return "Re: " + subject


def _reply_references(
    headers: Mapping[str, str],
) -> tuple[str | None, tuple[str, ...]]:
    """``In-Reply-To`` et ``References`` dérivés du message d'origine."""
    message_header = _optional_text(headers.get("message-id"))
    identifiers = (headers.get("references") or "").split()
    if message_header is not None and message_header not in identifiers:
        identifiers.append(message_header)
    return message_header, tuple(identifiers)


def _reply_recipients(
    headers: Mapping[str, str],
    *,
    self_address: str,
    reply_all: bool,
) -> tuple[list[str], list[str]]:
    """Destinataires du message d'origine, l'adresse du compte exclue."""
    sender = _header_recipients(headers.get("from", ""))
    if not sender:
        return [], []
    excluded = (self_address,) if self_address else ()
    to = _merge_recipients([sender], excluded=excluded)
    cc: list[tuple[str, str]] = []
    if reply_all:
        to = _merge_recipients(
            [sender, _header_recipients(headers.get("to", ""))],
            excluded=excluded,
        )
        cc = _merge_recipients(
            [_header_recipients(headers.get("cc", ""))],
            excluded=excluded,
            already=(address for _, address in to),
        )
    return _render_recipients(to), _render_recipients(cc)


def _header_recipients(value: str) -> list[tuple[str, str]]:
    """Couples (nom affiché, adresse) d'un en-tête, adresses vides écartées."""
    recipients: list[tuple[str, str]] = []
    for name, address in getaddresses([value]):
        address = address.strip()
        if address:
            recipients.append((name.strip(), address))
    return recipients


def _merge_recipients(
    groups: list[list[tuple[str, str]]],
    *,
    excluded: tuple[str, ...] = (),
    already: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """Fusionne des destinataires sans doublon d'adresse ni adresse exclue."""
    seen = {address.lower() for address in already}
    blocked = {address.lower() for address in excluded}
    merged: list[tuple[str, str]] = []
    for group in groups:
        for name, address in group:
            key = address.lower()
            if key in seen or key in blocked:
                continue
            seen.add(key)
            merged.append((name, address))
    return merged


def _render_recipients(recipients: list[tuple[str, str]]) -> list[str]:
    return [
        formataddr((name, address)) if name else address
        for name, address in recipients
    ]


async def _send_raw_message(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    *,
    raw: str,
    thread_id: str | None,
    prefix: str,
) -> Mapping[str, Any]:
    """``POST /messages`` avec ``raw`` ; ``threadId`` seulement pour une réponse."""
    payload: dict[str, object] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    url = settings.api_base_url + GMAIL_MESSAGES_PATH
    response = await _authorized_delivery(
        settings, account, trace, "POST", url, payload=payload, prefix=prefix
    )
    if response.status_code >= 400:
        raise _RequestFailure(
            _delivery_status_error(prefix, response.status_code),
            f"HTTP {response.status_code}",
        )
    return _delivery_json_body(response, prefix)


def _delivery_json_body(
    response: httpx2.Response, prefix: str
) -> Mapping[str, Any]:
    """Corps JSON objet d'un envoi ; tout le reste est une réponse illisible."""
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise _RequestFailure(
            prefix + _DELIVERY_UNREADABLE, "unreadable"
        ) from exc
    if not isinstance(body, dict):
        raise _RequestFailure(prefix + _DELIVERY_UNREADABLE, "unreadable")
    return body


def _compose_raw_message(
    *,
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: tuple[str, ...] = (),
) -> str:
    """Message ``text/plain`` UTF-8, encodé base64url comme l'exige Gmail."""
    message = EmailMessage()
    message["To"] = ", ".join(to)
    if cc:
        message["Cc"] = ", ".join(cc)
    if bcc:
        message["Bcc"] = ", ".join(bcc)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = " ".join(references)
    message["Subject"] = subject
    message.set_content(body, subtype="plain", charset="utf-8", cte="base64")
    return base64.urlsafe_b64encode(
        message.as_bytes(policy=email.policy.SMTP)
    ).decode("ascii")


def _addresses(value: object) -> list[str] | None:
    """Liste d'adresses non vides ; ``None`` quand la forme est invalide."""
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    addresses: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        addresses.append(item.strip())
    return addresses


async def _authorized_delivery(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    method: str,
    url: str,
    *,
    prefix: str,
    params: Mapping[str, object] | None = None,
    payload: Mapping[str, object] | None = None,
) -> httpx2.Response:
    """Requête porteuse du Bearer ; un 401 rafraîchit et rejoue une seule fois."""
    try:
        token = await _access_token(settings, account, trace)
    except _TokenError as exc:
        raise _AccountUnavailable(account.key) from exc

    response = await _delivery_request(
        method, url, token, prefix=prefix, params=params, payload=payload
    )
    if response.status_code != 401:
        return response

    try:
        token = await _access_token(settings, account, trace, force=True)
    except _TokenError as exc:
        raise _AccountUnavailable(account.key) from exc
    return await _delivery_request(
        method, url, token, prefix=prefix, params=params, payload=payload
    )


async def _delivery_request(
    method: str,
    url: str,
    token: str,
    *,
    prefix: str,
    params: Mapping[str, object] | None = None,
    payload: Mapping[str, object] | None = None,
) -> httpx2.Response:
    """Une requête d'envoi, ou une ``_RequestFailure`` en français."""
    try:
        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(REQUEST_TIMEOUT),
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            return await client.request(
                method,
                url,
                params=dict(params or {}),
                json=dict(payload) if payload is not None else None,
            )
    except asyncio.CancelledError:
        raise
    except httpx2.TimeoutException as exc:
        raise _RequestFailure(prefix + _DELIVERY_TIMEOUT, "timeout") from exc
    except httpx2.RequestError as exc:
        raise _RequestFailure(
            prefix + _DELIVERY_UNREACHABLE, type(exc).__name__
        ) from exc
    except Exception as exc:  # défense : le handler ne lève jamais
        raise _RequestFailure(
            prefix + _DELIVERY_UNREACHABLE, type(exc).__name__
        ) from exc


def _delivery_status_error(prefix: str, status: int) -> str:
    """Phrase française d'un statut Gmail pour l'envoi ou la réponse."""
    if status == 400:
        return prefix + _DELIVERY_VALIDATION
    if status == 401:
        return prefix + _DELIVERY_REFUSED
    if status == 403:
        return prefix + _DELIVERY_FORBIDDEN
    if status in (404, 410):
        return prefix + _DELIVERY_NOT_FOUND
    if status in (429, 529):
        return prefix + _DELIVERY_RATE
    if status >= 500:
        return prefix + f"le service Gmail est indisponible (réponse {status})."
    return prefix + f"le service Gmail a renvoyé une erreur (réponse {status})."


def _unknown_account_message(settings: _Settings, shown: str) -> str:
    return (
        f"Le compte « {shown} » est inconnu. "
        f"Comptes disponibles : {_account_names(settings)}."
    )


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
