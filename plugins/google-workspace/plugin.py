"""Plugin ``google-workspace`` : mails Gmail, agenda Google et contacts.

Le socle est celui de l'ADR-0021 (refresh tokens statiques) ; le carnet ajoute
l'API People de l'ADR-0022.

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

Le socle (comptes, jetons, HTTP, erreurs) est partagé : le plugin expose
neuf Tools, quatre pour Gmail (``gmail_search``, ``gmail_read``,
``gmail_send``, ``gmail_reply``), quatre pour l'agenda
(``calendar_list_events``, ``calendar_create_event``,
``calendar_update_event``, ``calendar_delete_event``) et un pour le carnet
(``contacts_search``). Le carnet vient de l'API People, sur un hôte distinct
réglé par ``people_base_url`` (ADR-0022).
"""

from __future__ import annotations

import asyncio
import base64
import email.policy
import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, getaddresses
from typing import Any, Iterable, Mapping
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
DEFAULT_PEOPLE_BASE_URL = "https://people.googleapis.com"
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

# Bornes de ``gmail_read`` : dix messages par défaut dans un fil, vingt au plus.
GMAIL_READ_DEFAULT = 10
GMAIL_READ_MAX = 20
GMAIL_READ_MIN = 1

# Un corps est rendu en texte et tronqué au-delà de six mille caractères
# (spec #135) ; ``truncated`` annonce la coupe au modèle.
GMAIL_READ_BODY_LIMIT = 6000

# Chemins Gmail. ``me`` désigne le compte porteur du jeton.
GMAIL_MESSAGES_PATH = "/gmail/v1/users/me/messages"
# ``messages.insert`` (POST /messages) n'accepte pas le scope ``gmail.send`` ;
# l'envoi passe par ``messages.send``, seul endpoint couvert par ce scope.
GMAIL_SEND_PATH = GMAIL_MESSAGES_PATH + "/send"
GMAIL_THREADS_PATH = "/gmail/v1/users/me/threads"

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

# Fenêtre par défaut de ``calendar_list_events`` : maintenant, puis sept jours.
CALENDAR_DEFAULT_WINDOW_DAYS = 7

# Bornes de ``calendar_list_events`` : dix événements par défaut, cinquante au plus.
CALENDAR_MAX_RESULTS_DEFAULT = 10
CALENDAR_MAX_RESULTS_MAX = 50
CALENDAR_MAX_RESULTS_MIN = 1

# Chemin Calendar ; l'identifiant d'agenda y est encodé séparément.
CALENDAR_EVENTS_BASE_PATH = "/calendar/v3/calendars"

# Carnet People API : deux gisements fusionnés par adresse (ADR-0022).
PEOPLE_ME_PATH = "/v1/people/me"
PEOPLE_CONNECTIONS_PATH = "/v1/people/me/connections"
OTHER_CONTACTS_PATH = "/v1/otherContacts"
CONTACTS_PERSON_FIELDS = "names,emailAddresses"

# Bornes de ``contacts_search`` : dix personnes par défaut, vingt-cinq au plus.
CONTACTS_MAX_RESULTS_DEFAULT = 10
CONTACTS_MAX_RESULTS_MAX = 25
CONTACTS_MAX_RESULTS_MIN = 1

# La sonde de démarrage ne retarde jamais l'activation plus de deux secondes.
CONTACTS_PROBE_TIMEOUT = 2.0

# Une recherche vocale ne pagine pas : on parcourt au plus quatre pages par
# gisement, puis ``truncated`` dit au modèle de faire préciser le nom.
CONTACTS_PAGE_SIZE = 500
CONTACTS_MAX_PAGES = 4

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

GMAIL_READ_DESCRIPTION = "Lit un message ou un fil Gmail en texte."

GMAIL_READ_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "message_id": {
            "type": "string",
            "description": (
                "Identifiant du message Gmail à lire ; exclusif avec thread_id."
            ),
        },
        "thread_id": {
            "type": "string",
            "description": (
                "Identifiant du fil Gmail à lire ; exclusif avec message_id."
            ),
        },
        "account": {
            "type": "string",
            "description": (
                "Clé du compte Google à utiliser ; le compte par défaut "
                "quand il est absent."
            ),
        },
        "max_messages": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "Nombre de messages lus dans un fil, de 1 à 20. Défaut 10.",
        },
    },
    "required": [],
}

GMAIL_SEND_DESCRIPTION = "Envoie un mail en texte brut."

GMAIL_SEND_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "to": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": (
                "Destinataires principaux, au moins un ; des adresses e-mail, "
                "jamais des noms."
            ),
        },
        "cc": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Destinataires en copie ; des adresses e-mail.",
        },
        "bcc": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Destinataires en copie cachée ; des adresses e-mail."
            ),
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

CONTACTS_SEARCH_DESCRIPTION = (
    "Cherche des personnes dans les contacts Google et rend leurs adresses."
)

CONTACTS_SEARCH_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Nom ou adresse à chercher dans les contacts Google.",
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
            "description": "Nombre de personnes rendues, de 1 à 25. Défaut 10.",
        },
    },
    "required": ["query"],
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
            "description": (
                "Nombre d'Événements d'agenda rendus, de 1 à 50. Défaut 10."
            ),
        },
        "page_token": {
            "type": "string",
            "description": "Jeton de page rendu par l'appel précédent.",
        },
        "calendar_id": {
            "type": "string",
            "description": "Identifiant de l'agenda. Défaut : « primary ».",
        },
        "query": {
            "type": "string",
            "description": "Recherche plein texte dans les Événements d'agenda.",
        },
    },
}

CALENDAR_CREATE_EVENT_DESCRIPTION = "Crée un événement d'agenda."

CALENDAR_CREATE_EVENT_PARAMETERS: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Titre de l'Événement d'agenda.",
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
            "description": "Description de l'Événement d'agenda.",
        },
        "location": {
            "type": "string",
            "description": "Lieu de l'Événement d'agenda.",
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
                "Identifiant de l'Événement d'agenda rendu par "
                "`calendar_list_events`."
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
                "Identifiant de l'Événement d'agenda rendu par "
                "`calendar_list_events`."
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
    "Tu disposes des Tools Google Workspace pour les mails Gmail, l'agenda "
    "Google et les contacts.\n"
    "\n"
    "COMPTES:\n"
    "- Comptes disponibles : {accounts}. Compte par défaut : {default}.\n"
    "- Utilise le compte que l'utilisateur nomme ; sinon le compte par défaut.\n"
    "- Si la demande peut viser plusieurs comptes et qu'aucun n'est nommé, "
    "demande lequel.\n"
    "- N'invente jamais un nom de compte.\n"
    "\n"
    "CONTACTS:\n"
    "- `contacts_search` d'abord quand l'utilisateur nomme une personne ; "
    "n'invente jamais une adresse.\n"
    "- Une personne avec une seule adresse : annonce l'adresse trouvée.\n"
    "- Une personne avec plusieurs adresses : lis-les à voix haute et demande "
    "laquelle, sans choisir.\n"
    "- Plusieurs personnes : lis les noms et les adresses, puis fais choisir.\n"
    "- Aucun résultat : dis-le et propose de donner l'adresse exacte ; ne "
    "cherche pas dans un autre compte sans le demander.\n"
    "\n"
    "MAILS:\n"
    "- `gmail_search` d'abord pour trouver un message ; n'invente jamais un "
    "identifiant.\n"
    "- `gmail_read` pour lire un message ou un fil ; ne lis pas un fil entier "
    "si l'utilisateur ne demande qu'un message.\n"
    "- `gmail_send` et `gmail_reply` : relis à voix haute destinataires, objet "
    "et corps, puis attends un « oui » explicite avant d'envoyer. N'envoie "
    "jamais de ta propre initiative.\n"
    "- Quand l'adresse vient de `contacts_search`, la relecture d'envoi inclut "
    "l'adresse résolue : ne demande pas une confirmation pour l'adresse puis "
    "une autre pour le message.\n"
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


@dataclass(frozen=True, slots=True)
class _StatusReasons:
    """Fragments français d'un statut HTTP, communs à tous les domaines."""

    validation: str
    forbidden: str
    not_found: str
    service: str
    not_found_statuses: tuple[int, ...] = (404,)


def _status_message(status: int, prefix: str, reasons: _StatusReasons) -> str:
    """Phrase française d'un statut HTTP, toujours préfixée par l'action."""
    if status == 400:
        return prefix + reasons.validation
    if status == 401:
        return prefix + "l'accès au compte a été refusé. Réessaie dans un instant."
    if status == 403:
        return prefix + reasons.forbidden
    if status in reasons.not_found_statuses:
        return prefix + reasons.not_found
    if status in (429, 529):
        return prefix + "trop de requêtes ont été envoyées. Réessaie dans un instant."
    if status >= 500:
        return (
            prefix
            + f"le service {reasons.service} est indisponible "
            f"(réponse {status})."
        )
    return (
        prefix
        + f"le service {reasons.service} a renvoyé une erreur "
        f"(réponse {status})."
    )


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
_SEARCH_STATUS_REASONS = _StatusReasons(
    validation="la requête a été refusée. Vérifie la syntaxe de recherche.",
    forbidden="l'accès à ce compte est refusé.",
    not_found="le message ou la boîte demandée est introuvable.",
    service="Gmail",
)

_READ_PREFIX = "La lecture du message Gmail n'a pas abouti : "

_READ_UNREACHABLE_MESSAGE = (
    _READ_PREFIX + "le service Gmail est injoignable. Réessaie dans un instant."
)
_READ_TIMEOUT_MESSAGE = (
    _READ_PREFIX
    + "le service Gmail n'a pas répondu à temps. Réessaie dans un instant."
)
_READ_UNREADABLE_MESSAGE = (
    _READ_PREFIX
    + "le service Gmail a renvoyé une réponse illisible. Réessaie dans un instant."
)
_READ_STATUS_REASONS = _StatusReasons(
    validation=(
        "la requête a été refusée. Vérifie l'identifiant du message ou du fil."
    ),
    forbidden="l'accès à ce compte est refusé.",
    not_found="le message ou le fil demandé est introuvable.",
    service="Gmail",
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
_CALENDAR_STATUS_REASONS = _StatusReasons(
    validation=(
        "la période ou la requête a été refusée. "
        "Vérifie les dates demandées."
    ),
    forbidden="l'accès à cet agenda est refusé.",
    not_found="l'agenda demandé est introuvable.",
    service="Agenda",
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
_DELIVERY_STATUS_REASONS = _StatusReasons(
    validation="la requête a été refusée.",
    forbidden="l'accès à ce compte est refusé.",
    not_found="le message demandé est introuvable.",
    service="Gmail",
)

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
    _CALENDAR_UPDATE_PREFIX + "l'identifiant de l'Événement d'agenda est requis."
)
_CALENDAR_DELETE_VALIDATION_MESSAGE = (
    _CALENDAR_DELETE_PREFIX + "l'identifiant de l'Événement d'agenda est requis."
)
_CALENDAR_DELETE_CONFIRM_MESSAGE = (
    _CALENDAR_DELETE_PREFIX
    + "une confirmation explicite est requise (confirm=true)."
)
_CALENDAR_WRITE_STATUS_REASONS = _StatusReasons(
    validation=(
        "les données de l'Événement d'agenda ont été refusées. "
        "Vérifie les dates et le titre."
    ),
    forbidden="l'accès à cet agenda est refusé.",
    not_found="l'Événement d'agenda ou l'agenda est introuvable.",
    service="Agenda",
)

# Phrases propres aux contacts : le carnet vit sur l'API People (ADR-0022).
_CONTACTS_PREFIX = "La recherche dans les contacts n'a pas abouti : "

_CONTACTS_UNREACHABLE_MESSAGE = (
    _CONTACTS_PREFIX
    + "le service Contacts est injoignable. Réessaie dans un instant."
)
_CONTACTS_TIMEOUT_MESSAGE = (
    _CONTACTS_PREFIX
    + "le service Contacts n'a pas répondu à temps. "
    "Réessaie dans un instant."
)
_CONTACTS_UNREADABLE_MESSAGE = (
    _CONTACTS_PREFIX
    + "le service Contacts a renvoyé une réponse illisible. "
    "Réessaie dans un instant."
)
_CONTACTS_STATUS_REASONS = _StatusReasons(
    validation="la requête a été refusée. Vérifie le nom cherché.",
    forbidden="l'accès au carnet de ce compte est refusé.",
    not_found="le carnet demandé est introuvable.",
    service="Contacts",
)

# Raisons Google d'un 403 réparable par une réautorisation, jamais un texte
# libre : seule cette liste devient une phrase dédiée.
_CONTACTS_SCOPE_REASONS = frozenset(
    {
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        "SERVICE_DISABLED",
        "accessNotConfigured",
        "insufficientPermissions",
    }
)


def _contacts_scope_message(account: str) -> str:
    """Phrase d'un carnet inaccessible faute de scope ou d'API activée."""
    return (
        _CONTACTS_PREFIX
        + f"le carnet du compte « {account} » n'est pas accessible : "
        "active l'API People et réautorise le compte, puis réessaie."
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
    client_id: str
    client_secret: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class _Settings:
    """Configuration validée du plugin, comptes compris."""

    accounts: tuple[_Account, ...]
    default_account: str
    api_base_url: str
    people_base_url: str
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
        token = await _access_token(settings, account, trace, force=True)
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
    try:
        await asyncio.wait_for(
            _probe_contacts(settings, account, trace, token),
            timeout=CONTACTS_PROBE_TIMEOUT,
        )
    except TimeoutError:
        pass
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
        people_base_url=text(
            "people_base_url", DEFAULT_PEOPLE_BASE_URL
        ).rstrip("/"),
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


def _folded(text: str) -> str:
    """Retire les accents (NFKD) ; la casse reste à l'appelant."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )


def _account_key(label: str) -> str:
    """Clé de compte : minuscules, accents retirés, non-alphanumériques en ``-``."""
    replaced = "".join(
        character if character.isalnum() else "-"
        for character in _folded(label).lower()
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
    return _status_message(status, _SEARCH_PREFIX, _SEARCH_STATUS_REASONS)


# -- HTTP de ``gmail_read`` ---------------------------------------------------


async def _read_get(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    url: str,
    params: Mapping[str, object],
) -> httpx2.Response:
    """GET réutilisant le socle (jeton, 401) avec les phrases de lecture."""
    try:
        return await _authorized_get(settings, account, trace, url, params)
    except _RequestFailure as exc:
        raise _RequestFailure(_read_transport_message(exc.status), exc.status) from exc


def _read_transport_message(status: str) -> str:
    """Phrase de lecture d'une panne de transport, jamais un détail brut."""
    if status == "timeout":
        return _READ_TIMEOUT_MESSAGE
    if status == "unreadable":
        return _READ_UNREADABLE_MESSAGE
    return _READ_UNREACHABLE_MESSAGE


def _read_json_body(response: httpx2.Response) -> Mapping[str, Any]:
    """Corps JSON objet, relu avec la phrase d'illisibilité de lecture."""
    try:
        return _json_body(response)
    except _RequestFailure as exc:
        raise _RequestFailure(_READ_UNREADABLE_MESSAGE, exc.status) from exc


def _read_status_error(status: int) -> str:
    """Phrase française d'un statut Gmail rencontré en lecture."""
    return _status_message(status, _READ_PREFIX, _READ_STATUS_REASONS)


# -- Tool ``gmail_search`` ----------------------------------------------------


def _tool_bindings(
    settings: _Settings, trace: Trace
) -> tuple[ToolBinding, ...]:
    """Les neuf Tools du plugin : quatre Gmail, un carnet, quatre Calendar."""
    return (
        _gmail_search_tool(settings, trace),
        _gmail_read_tool(settings, trace),
        _gmail_send_tool(settings, trace),
        _gmail_reply_tool(settings, trace),
        _contacts_search_tool(settings, trace),
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
        return _unknown_account_failure(
            settings, arguments, trace, tool, started
        )

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
                    values[name.lower()] = _decoded_header(value)
    return values


def _decoded_header(value: str) -> str:
    """Décode un en-tête RFC 2047 ; un encodage cassé rend la valeur d'origine."""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


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
        return _unknown_account_failure(
            settings, arguments, trace, tool, started
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
    named = next((value for value in to if not _address_like(value)), None)
    if named is not None:
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_to",
            _SEND_PREFIX + _not_an_address_message(),
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
    copied = next(
        (
            value
            for values in (cc, bcc)
            for value in values
            if not _address_like(value)
        ),
        None,
    )
    if copied is not None:
        return _fail(
            trace,
            tool,
            account.key,
            started,
            "invalid_recipient",
            _SEND_PREFIX + _not_an_address_message(copy=True),
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
        return _unknown_account_failure(
            settings, arguments, trace, tool, started
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
    """``POST /messages/send`` avec ``raw`` ; ``threadId`` pour une réponse."""
    payload: dict[str, object] = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    url = settings.api_base_url + GMAIL_SEND_PATH
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


def _address_like(value: str) -> bool:
    """Vrai pour une adresse e-mail, éventuellement sous forme « Nom <adresse> ».

    Un nom seul, une liste de plusieurs adresses ou une chaîne avec un espace
    dans la partie adresse est refusé : le modèle doit d'abord résoudre la
    personne avec ``contacts_search``.
    """
    try:
        parsed = getaddresses([value])
    except Exception:
        return False
    if len(parsed) != 1:
        return False
    _, address = parsed[0]
    return (
        bool(address)
        and "@" in address
        and not any(character.isspace() for character in address)
    )


def _not_an_address_message(*, copy: bool = False) -> str:
    """Phrase d'un nom reçu à la place d'une adresse ; jamais la valeur."""
    role = "en copie " if copy else ""
    return (
        f"un destinataire {role}n'est pas une adresse e-mail : utilise "
        "`contacts_search` pour trouver l'adresse de la personne."
    )


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
    return _status_message(status, prefix, _DELIVERY_STATUS_REASONS)


def _unknown_account_message(settings: _Settings, shown: str) -> str:
    return (
        f"Le compte « {shown} » est inconnu. "
        f"Comptes disponibles : {_account_names(settings)}."
    )


def _unknown_account_failure(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
    tool: str,
    started: float,
) -> Mapping[str, object]:
    """Refus unique d'un compte inconnu, avant tout appel réseau."""
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


# -- Tool ``contacts_search`` -------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidate:
    """Une personne candidate, déjà réduite à son nom et à ses adresses."""

    name: str
    emails: tuple[tuple[str, str], ...]
    source: str


@dataclass(slots=True)
class _Group:
    """Personnes fusionnées autour d'une même adresse, noms par provenance."""

    carnet_name: str = ""
    other_name: str = ""
    source: str = "autres"
    emails: list[tuple[str, str]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        """Le nom du carnet prime sur celui des autres contacts."""
        return self.carnet_name or self.other_name

    def add(self, candidate: _Candidate) -> None:
        """Ajoute une personne au groupe sans répéter une adresse déjà vue."""
        if candidate.source == "carnet":
            self.source = "carnet"
            self.carnet_name = self.carnet_name or candidate.name
        else:
            self.other_name = self.other_name or candidate.name
        for address, kind in candidate.emails:
            key = _normalized(address)
            if key in self.seen:
                continue
            self.seen.add(key)
            self.emails.append((address, kind))


def _contacts_search_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``contacts_search`` et son handler : un appel ne lève jamais."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _contacts_search(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="contacts_search",
            description=CONTACTS_SEARCH_DESCRIPTION,
            parameters=CONTACTS_SEARCH_PARAMETERS,
        ),
        handler=handle,
    )


async def _contacts_search(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Cherche des personnes dans les deux gisements, sans jamais lever."""
    tool = "contacts_search"
    started = asyncio.get_running_loop().time()

    account = _resolve_account(settings, arguments.get("account"))
    if account is None:
        return _unknown_account_failure(
            settings, arguments, trace, tool, started
        )

    query = arguments.get("query")
    tokens = _contact_tokens(query if isinstance(query, str) else "")
    if not isinstance(query, str) or not query.strip() or not tokens:
        message = _CONTACTS_PREFIX + "la requête est vide."
        return _fail(trace, tool, account.key, started, "invalid_query", message)

    max_results = _bounded_int(
        arguments.get("max_results"),
        default=CONTACTS_MAX_RESULTS_DEFAULT,
        minimum=CONTACTS_MAX_RESULTS_MIN,
        maximum=CONTACTS_MAX_RESULTS_MAX,
    )

    try:
        (saved, cut_saved), (others, cut_others) = await asyncio.gather(
            _fetch_people(
                settings,
                account,
                trace,
                path=PEOPLE_CONNECTIONS_PATH,
                params={
                    "personFields": CONTACTS_PERSON_FIELDS,
                    "pageSize": CONTACTS_PAGE_SIZE,
                },
                key="connections",
                source="carnet",
            ),
            _fetch_people(
                settings,
                account,
                trace,
                path=OTHER_CONTACTS_PATH,
                params={
                    "readMask": CONTACTS_PERSON_FIELDS,
                    "pageSize": CONTACTS_PAGE_SIZE,
                },
                key="otherContacts",
                source="autres",
            ),
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
            _CONTACTS_UNREADABLE_MESSAGE,
        )

    people, truncated = _select_contacts(
        saved, others, tokens, max_results
    )
    payload: dict[str, object] = {
        "status": "ok",
        "account": account.key,
        "results": people,
        "truncated": truncated or cut_saved or cut_others,
    }
    _trace_tool(trace, tool, account.key, started, "ok", len(people))
    return payload


async def _fetch_people(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    *,
    path: str,
    params: Mapping[str, object],
    key: str,
    source: str,
) -> tuple[list[_Candidate], bool]:
    """Lit un gisement page après page, borné par ``CONTACTS_MAX_PAGES``."""
    url = settings.people_base_url + path
    candidates: list[_Candidate] = []
    page_token: str | None = None
    pages = 0
    truncated = False
    while True:
        query = dict(params)
        if page_token is not None:
            query["pageToken"] = page_token
        response = await _contacts_get(settings, account, trace, url, query)
        if response.status_code >= 400:
            message, status = _contacts_status_error(response, account.key)
            raise _RequestFailure(message, status)
        body = _contacts_json_body(response)
        items = body.get(key)
        if isinstance(items, list):
            for item in items:
                candidate = _candidate(item, source)
                if candidate is not None:
                    candidates.append(candidate)
        page_token = _optional_text(body.get("nextPageToken"))
        pages += 1
        if page_token is None:
            break
        if pages >= CONTACTS_MAX_PAGES:
            truncated = True
            break
    return candidates, truncated


def _candidate(item: object, source: str) -> _Candidate | None:
    """Personne d'une réponse People, écartée si elle n'a aucune adresse."""
    if not isinstance(item, dict):
        return None
    emails = _email_addresses(item.get("emailAddresses"))
    if not emails:
        return None
    return _Candidate(
        name=_primary_name(item.get("names")),
        emails=tuple(emails),
        source=source,
    )


def _primary_name(names: object) -> str:
    """Nom principal d'une fiche People ; le premier non vide sinon."""
    if not isinstance(names, list):
        return ""
    first = ""
    for entry in names:
        if not isinstance(entry, dict):
            continue
        display = _text(entry.get("displayName")).strip()
        if not display:
            continue
        metadata = entry.get("metadata")
        if isinstance(metadata, dict) and metadata.get("primary") is True:
            return display
        if not first:
            first = display
    return first


def _email_addresses(raw: object) -> list[tuple[str, str]]:
    """Adresses d'une fiche People, dédoublonnées sur la forme normalisée."""
    if not isinstance(raw, list):
        return []
    addresses: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        address = _text(entry.get("value")).strip()
        if not address:
            continue
        key = _normalized(address)
        if key in seen:
            continue
        seen.add(key)
        addresses.append((address, _text(entry.get("type")).strip()))
    return addresses


def _select_contacts(
    saved: list[_Candidate],
    others: list[_Candidate],
    tokens: list[str],
    max_results: int,
) -> tuple[list[dict[str, object]], bool]:
    """Fusionne, filtre, trie et borne les personnes pour le modèle."""
    merged = _merged_contacts(saved + others)
    matches = [
        candidate
        for candidate in merged
        if _candidate_matches(candidate, tokens)
    ]
    matches.sort(key=_contact_order)
    truncated = len(matches) > max_results
    return [
        {
            "name": candidate.name,
            "emails": [
                {"address": address, "type": kind}
                for address, kind in candidate.emails
            ],
            "source": candidate.source,
        }
        for candidate in matches[:max_results]
    ], truncated


def _merged_contacts(candidates: list[_Candidate]) -> list[_Candidate]:
    """Fusionne les personnes qui partagent au moins une adresse (ADR-0022)."""
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        """Racine d'une adresse, avec compression de chemin."""
        root = key
        while parent[root] != root:
            root = parent[root]
        while parent[key] != root:
            parent[key], key = root, parent[key]
        return root

    def union(left: str, right: str) -> None:
        """Réunit deux adresses dans le même groupe."""
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for candidate in candidates:
        keys = [_normalized(address) for address, _ in candidate.emails]
        for key in keys:
            parent.setdefault(key, key)
        for key in keys[1:]:
            union(keys[0], key)

    groups: dict[str, _Group] = {}
    order: list[str] = []
    for candidate in candidates:
        root = find(_normalized(candidate.emails[0][0]))
        group = groups.get(root)
        if group is None:
            group = _Group()
            groups[root] = group
            order.append(root)
        group.add(candidate)

    return [
        _Candidate(
            name=groups[root].name,
            emails=tuple(groups[root].emails),
            source=groups[root].source,
        )
        for root in order
    ]


def _normalized(text: str) -> str:
    """Forme de comparaison : accents retirés, casse ignorée."""
    return _folded(text).casefold().strip()


def _contact_tokens(query: str) -> list[str]:
    """Mots normalisés d'une recherche ; vide pour une requête sans mot."""
    return [token for token in _normalized(query).split() if token]


def _candidate_matches(candidate: _Candidate, tokens: list[str]) -> bool:
    """Vrai si chaque mot de la requête apparaît dans le nom ou une adresse."""
    haystack = _normalized(
        " ".join(
            [candidate.name]
            + [address for address, _ in candidate.emails]
        )
    )
    return all(token in haystack for token in tokens)


def _contact_order(candidate: _Candidate) -> tuple[str, str]:
    """Tri stable : nom, puis première adresse."""
    return (
        _normalized(candidate.name),
        _normalized(candidate.emails[0][0]),
    )


async def _contacts_get(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    url: str,
    params: Mapping[str, object],
) -> httpx2.Response:
    """GET réutilisant le socle (jeton, 401) avec les phrases du carnet."""
    try:
        return await _authorized_get(
            settings, account, trace, url, params
        )
    except _RequestFailure as exc:
        raise _RequestFailure(
            _contacts_transport_message(exc.status), exc.status
        ) from exc


def _contacts_transport_message(status: str) -> str:
    """Phrase du carnet pour une panne de transport, jamais un détail brut."""
    if status == "timeout":
        return _CONTACTS_TIMEOUT_MESSAGE
    if status == "unreadable":
        return _CONTACTS_UNREADABLE_MESSAGE
    return _CONTACTS_UNREACHABLE_MESSAGE


def _contacts_json_body(response: httpx2.Response) -> Mapping[str, Any]:
    """Corps JSON objet, relu avec la phrase d'illisibilité du carnet."""
    try:
        return _json_body(response)
    except _RequestFailure as exc:
        raise _RequestFailure(
            _CONTACTS_UNREADABLE_MESSAGE, exc.status
        ) from exc


def _contacts_status_error(
    response: httpx2.Response, account: str
) -> tuple[str, str]:
    """Phrase et statut d'un refus People, scope manquant reconnu à part."""
    if response.status_code == 403:
        reason = _scope_reason(response)
        if reason is not None:
            return _contacts_scope_message(account), "insufficient_scope"
    return (
        _status_message(
            response.status_code, _CONTACTS_PREFIX, _CONTACTS_STATUS_REASONS
        ),
        f"HTTP {response.status_code}",
    )


def _scope_reason(response: httpx2.Response) -> str | None:
    """Raison Google connue d'un 403 réparable, ou ``None``."""
    try:
        body = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    reasons: list[str] = []
    status = error.get("status")
    if isinstance(status, str):
        reasons.append(status)
    for field, key in (("errors", "reason"), ("details", "reason")):
        entries = error.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                reason = entry.get(key)
                if isinstance(reason, str):
                    reasons.append(reason)
    for reason in reasons:
        if reason in _CONTACTS_SCOPE_REASONS:
            return reason
    return None


async def _probe_contacts(
    settings: _Settings, account: _Account, trace: Trace, token: str
) -> None:
    """Sonde non bloquante : un scope manquant devient un avertissement."""
    url = settings.people_base_url + PEOPLE_ME_PATH
    try:
        response = await _get(url, token, {"personFields": "names"})
    except asyncio.CancelledError:
        raise
    except Exception:
        return
    if response.status_code != 403:
        return
    reason = _scope_reason(response)
    if reason is None:
        return
    trace(
        "ContactsScopeMissing",
        {"account": account.key, "reason": reason},
        level="warning",
    )


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
        return _unknown_account_failure(
            settings, arguments, trace, tool, started
        )

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
    return _status_message(status, _CALENDAR_PREFIX, _CALENDAR_STATUS_REASONS)


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
        return _unknown_account_failure(
            settings, arguments, trace, tool, started
        )

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
        return _unknown_account_failure(
            settings, arguments, trace, tool, started
        )

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
        return _unknown_account_failure(
            settings, arguments, trace, tool, started
        )

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
    """Borne ``date`` ou ``dateTime`` ; une fin de journée entière est exclue.

    Une valeur ``AAAA-MM-JJ`` suffit à faire une journée entière : le drapeau
    ``all_day`` n'est requis que pour exiger ce format.
    """
    stripped = value.strip()
    try:
        moment = date.fromisoformat(stripped)
    except ValueError as exc:
        if all_day:
            raise _RequestFailure(
                prefix
                + "la date de journée entière est invalide "
                "(AAAA-MM-JJ attendu).",
                "invalid_date",
            ) from exc
        return {"dateTime": value}
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
    return _status_message(status, prefix, _CALENDAR_WRITE_STATUS_REASONS)


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


# -- Tool ``gmail_read`` ------------------------------------------------------


def _gmail_read_tool(settings: _Settings, trace: Trace) -> ToolBinding:
    """Déclare ``gmail_read`` et son handler : un appel ne lève jamais."""

    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return await _gmail_read(settings, arguments, trace)

    return ToolBinding(
        spec=ToolSpec(
            name="gmail_read",
            description=GMAIL_READ_DESCRIPTION,
            parameters=GMAIL_READ_PARAMETERS,
        ),
        handler=handle,
    )


async def _gmail_read(
    settings: _Settings,
    arguments: Mapping[str, object],
    trace: Trace,
) -> Mapping[str, object]:
    """Lit un message ou un fil et rend son texte, sans jamais lever."""
    tool = "gmail_read"
    started = asyncio.get_running_loop().time()

    account = _resolve_account(settings, arguments.get("account"))
    if account is None:
        return _unknown_account_failure(
            settings, arguments, trace, tool, started
        )

    message_id = _optional_text(arguments.get("message_id"))
    thread_id = _optional_text(arguments.get("thread_id"))
    if message_id is not None and thread_id is not None:
        message = (
            _READ_PREFIX
            + "indique un seul identifiant, message_id ou thread_id, pas les deux."
        )
        return _fail(trace, tool, account.key, started, "invalid_identifier", message)
    if message_id is None and thread_id is None:
        message = _READ_PREFIX + "indique un message_id ou un thread_id."
        return _fail(trace, tool, account.key, started, "invalid_identifier", message)

    max_messages = _bounded_int(
        arguments.get("max_messages"),
        default=GMAIL_READ_DEFAULT,
        minimum=GMAIL_READ_MIN,
        maximum=GMAIL_READ_MAX,
    )

    try:
        if message_id is not None:
            messages, parent_thread_id, capped = await _read_message_by_id(
                settings, account, trace, message_id
            )
        else:
            messages, parent_thread_id, capped = await _read_thread_by_id(
                settings,
                account,
                trace,
                thread_id if thread_id is not None else "",
                max_messages=max_messages,
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
            _READ_UNREADABLE_MESSAGE,
        )

    payload: dict[str, object] = {
        "status": "ok",
        "account": account.key,
        "messages": messages,
    }
    if parent_thread_id:
        payload["thread_id"] = parent_thread_id
    if capped or any(bool(item["truncated"]) for item in messages):
        payload["truncated"] = True
    _trace_tool(trace, tool, account.key, started, "ok", len(messages))
    return payload


async def _read_message_by_id(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    identifier: str,
) -> tuple[list[dict[str, object]], str | None, bool]:
    """``GET /messages/{id}?format=full`` : un message, jamais son fil entier."""
    url = settings.api_base_url + GMAIL_MESSAGES_PATH + "/" + identifier
    body = await _read_object(settings, account, trace, url, {"format": "full"})
    message = _read_message(body, fallback_id=identifier)
    return [message], _optional_text(body.get("threadId")), False


async def _read_thread_by_id(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    identifier: str,
    *,
    max_messages: int,
) -> tuple[list[dict[str, object]], str | None, bool]:
    """``GET /threads/{id}?format=full``, borné à ``max_messages`` messages."""
    url = settings.api_base_url + GMAIL_THREADS_PATH + "/" + identifier
    body = await _read_object(settings, account, trace, url, {"format": "full"})
    listed = body.get("messages")
    items = listed if isinstance(listed, list) else []
    messages = [
        _read_message(item)
        for item in items[:max_messages]
        if isinstance(item, dict)
    ]
    thread_id = _optional_text(body.get("id")) or identifier
    return messages, thread_id, len(items) > max_messages


async def _read_object(
    settings: _Settings,
    account: _Account,
    trace: Trace,
    url: str,
    params: Mapping[str, object],
) -> Mapping[str, Any]:
    """GET porteur du jeton, puis corps JSON ; les statuts deviennent des phrases."""
    response = await _read_get(settings, account, trace, url, params)
    if response.status_code >= 400:
        raise _RequestFailure(
            _read_status_error(response.status_code),
            f"HTTP {response.status_code}",
        )
    return _read_json_body(response)


def _read_message(
    message: Mapping[str, Any], *, fallback_id: str = ""
) -> dict[str, object]:
    """Un message Gmail complet rendu en texte, pièces jointes nommées seulement."""
    headers = _headers(message)
    payload = message.get("payload")
    body, attachments = _read_payload(
        payload if isinstance(payload, dict) else {}
    )
    body, truncated = _truncate_body(body)
    return {
        "id": _text(message.get("id")) or fallback_id,
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "body": body,
        "truncated": truncated,
        "attachments": attachments,
    }


def _read_payload(
    payload: Mapping[str, Any],
) -> tuple[str, list[str]]:
    """Corps textuel d'une charge Gmail et noms des pièces jointes.

    Le ``text/plain`` l'emporte ; à défaut, le HTML est nettoyé. Une partie
    portant un nom de fichier est une pièce jointe : son nom est rendu, son
    contenu n'est jamais décodé.
    """
    plain: list[str] = []
    rendered_html: list[str] = []
    attachments: list[str] = []
    for part in _payload_parts(payload):
        filename = part.get("filename")
        if isinstance(filename, str) and filename.strip():
            name = filename.strip()
            if name not in attachments:
                attachments.append(name)
            continue
        text = _part_text(part)
        if not text:
            continue
        if part.get("mimeType") == "text/plain":
            plain.append(text)
        elif part.get("mimeType") == "text/html":
            rendered_html.append(text)

    if plain:
        body = "\n\n".join(segment.strip() for segment in plain if segment.strip())
    else:
        cleaned = [
            _clean_html(segment)
            for segment in rendered_html
            if segment.strip()
        ]
        body = "\n\n".join(segment for segment in cleaned if segment)
    return body, attachments


def _payload_parts(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Toutes les parties d'une charge, dans l'ordre du document."""
    parts: list[Mapping[str, Any]] = [payload]
    children = payload.get("parts")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                parts.extend(_payload_parts(child))
    return parts


def _part_text(part: Mapping[str, Any]) -> str:
    """Texte décodé d'une partie Gmail ; un corps absent ou illisible est vide."""
    body = part.get("body")
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    if not isinstance(data, str) or not data.strip():
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (TypeError, ValueError):
        return ""
    return decoded.decode("utf-8", "replace")


_HTML_SCRIPT_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_HTML_BREAK_RE = re.compile(
    r"<br\s*/?>|</p\s*>|</div\s*>|</li\s*>|</tr\s*>", re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_SPACES_RE = re.compile(r"[ \t\f\v]+")
_HTML_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _clean_html(raw: str) -> str:
    """HTML lisible : scripts retirés, balises coupées, entités décodées."""
    without_scripts = _HTML_SCRIPT_RE.sub(" ", raw)
    with_breaks = _HTML_BREAK_RE.sub("\n", without_scripts)
    without_tags = _HTML_TAG_RE.sub("", with_breaks)
    text = html.unescape(without_tags).replace("\u00a0", " ")
    lines = [_HTML_SPACES_RE.sub(" ", line).strip() for line in text.splitlines()]
    joined = _HTML_BLANK_LINES_RE.sub("\n\n", "\n".join(lines))
    return joined.strip()


def _truncate_body(body: str) -> tuple[str, bool]:
    """Coupe le corps à la limite annoncée et dit si une coupe a eu lieu."""
    if len(body) <= GMAIL_READ_BODY_LIMIT:
        return body, False
    return body[:GMAIL_READ_BODY_LIMIT], True
