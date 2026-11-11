"""Tests du plugin ``google-workspace`` : socle multi-comptes et recherche Gmail.

Chaque test traverse la couture réelle : App → faux Gemini Live → registre →
plugin copié → faux serveur Google (token OAuth, Gmail). Rien n'est simulé en
dehors du modèle et de Google : les Tools, l'instruction, les Traces et les
phrases d'erreur s'observent à la frontière. Aucun helper privé du plugin n'est
touché.

Le fichier est organisé par section : manifeste et secrets, validation de la
configuration, démarrage et échange OAuth, sélection du compte, contrat de
``gmail_search``, cache et rafraîchissement du jeton, pannes HTTP, contrat de
``calendar_list_events``, contrat des écritures d'agenda, contrat de
``contacts_search`` (carnet, autres contacts, choix de l'adresse), puis
vérification transverse (instruction, Console, Traces sans fuite).

Les tickets #137 à #141 étendent ce fichier ; les sections existantes laissent
la place aux Tools Gmail puis Calendar sans réorganiser le socle.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from testkit import RecordingTraceSink, wait_until
from google_stub import GoogleStub
from google_workspace_harness import (
    ACCESS_TOKEN_PERSO,
    ACCESS_TOKEN_PRO,
    CLIENT_ID_PERSO,
    CLIENT_ID_PRO,
    CLIENT_ID_VARIABLE,
    CLIENT_IDS,
    CLIENT_SECRET_PERSO,
    CLIENT_SECRET_PRO,
    CLIENT_SECRET_VARIABLE,
    CLIENT_SECRETS,
    PLUGIN_DIR,
    REFRESH_TOKEN_PERSO,
    REFRESH_TOKEN_PRO,
    REFRESH_TOKEN_VARIABLE,
    REFRESH_TOKENS,
    build_plugins,
    call_tool,
    calendar_event,
    calendar_event_list,
    declared,
    gmail_message_list,
    gmail_metadata,
    gmail_read_message,
    gmail_read_thread,
    gmail_reply_source,
    gmail_sent_message,
    google_workspace_session,
    install_google_workspace,
    live_session,
    other_contacts,
    people_connections,
    people_person,
    program_tokens,
    running_app,
    states,
    wait_for_state,
)
from server.adapters.plugins import PluginLoader
from server.adapters.plugins.loader import PluginLoadError
from server.app.secrets import EnvSecretResolver
from server.app.trace import FileTraceSink
from server.core.plugins import PluginRegistry

# ---------------------------------------------------------------------------
# Constantes partagées
# ---------------------------------------------------------------------------

GMAIL_SEARCH_DESCRIPTION = (
    "Cherche des messages Gmail avec la syntaxe de recherche Gmail et rend "
    "leurs métadonnées."
)

GMAIL_SEARCH_PARAMETERS: dict[str, Any] = {
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

GMAIL_READ_PARAMETERS: dict[str, Any] = {
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

CALENDAR_LIST_EVENTS_DESCRIPTION = (
    "Liste les événements d'agenda d'une période."
)

CALENDAR_LIST_EVENTS_PARAMETERS: dict[str, Any] = {
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

CALENDAR_CREATE_EVENT_PARAMETERS: dict[str, Any] = {
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

CALENDAR_UPDATE_EVENT_PARAMETERS: dict[str, Any] = {
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

CALENDAR_DELETE_EVENT_PARAMETERS: dict[str, Any] = {
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

# En-têtes RFC 2047 : la valeur décodée est la seule rendue au modèle.
ENCODED_SUBJECT = "=?utf-8?B?RMOpamV1bmVyIGpldWRp?="  # « Déjeuner jeudi »
ENCODED_SENDER = (
    "=?utf-8?Q?Camille_=C3=A9?= <camille@example.test>"  # « Camille é »
)
MALFORMED_SUBJECT = "=?utf-8?Q?=FF?="

CONTACTS_SEARCH_DESCRIPTION = (
    "Cherche des personnes dans les contacts Google et rend leurs adresses ; "
    "une faute d'orthographe légère est tolérée."
)

CONTACTS_SEARCH_PARAMETERS: dict[str, Any] = {
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
    "- Un retour avec `approximate: true` : le nom a été rapproché d'une "
    "orthographe voisine ; lis à voix haute le nom et l'adresse trouvés, puis "
    "fais confirmer avant d'envoyer.\n"
    "- Aucun résultat : dis-le et propose de donner l'adresse exacte ou "
    "d'épeler le nom ; ne cherche pas dans un autre compte sans le demander.\n"
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

INSTRUCTION = INSTRUCTION_TEMPLATE.replace("{accounts}", "pro, perso").replace(
    "{default}", "perso"
)

MESSAGES_PATH = "/gmail/v1/users/me/messages"
SEND_PATH = f"{MESSAGES_PATH}/send"
THREADS_PATH = "/gmail/v1/users/me/threads"
CALENDAR_EVENTS_BASE = "/calendar/v3/calendars"
CALENDAR_PRIMARY_PATH = f"{CALENDAR_EVENTS_BASE}/primary/events"
PEOPLE_ME_PATH = "/v1/people/me"
PEOPLE_CONNECTIONS_PATH = "/v1/people/me/connections"
PEOPLE_OTHER_PATH = "/v1/otherContacts"
TOKEN_SECRETS = (
    CLIENT_ID_PRO,
    CLIENT_ID_PERSO,
    CLIENT_SECRET_PRO,
    CLIENT_SECRET_PERSO,
    REFRESH_TOKEN_PRO,
    REFRESH_TOKEN_PERSO,
)


def _traces_json(sink: RecordingTraceSink) -> str:
    return json.dumps([entry.payload for entry in sink.entries])


def _load(loader: PluginLoader, registry: PluginRegistry | None = None) -> None:
    asyncio.run(
        loader.load(
            registry if registry is not None else PluginRegistry(),
            push_event=lambda source, text: None,
        )
    )


# ---------------------------------------------------------------------------
# Manifeste et secrets
# ---------------------------------------------------------------------------


def test_manifest_declares_the_google_identity_and_static_requirements() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    assert manifest == {
        "api_version": 1,
        "name": "google-workspace",
        "version": "0.2.0",
        "description": (
            "Mails Gmail, agenda Google et contacts : lecture, envoi, "
            "événements d'agenda"
        ),
        "platform": "server",
        "entrypoint": "plugin.py",
        "config": [
            {"key": "accounts", "type": "string", "required": True},
            {"key": "default_account", "type": "string"},
            {"key": "api_base_url", "type": "url"},
            {"key": "people_base_url", "type": "url"},
            {"key": "token_url", "type": "url"},
        ],
        "secrets": [
            {"key": "client_id", "required": True},
            {"key": "client_secret", "required": True},
            {"key": "refresh_token", "required": True},
        ],
    }


@pytest.mark.parametrize(
    "variable",
    [CLIENT_ID_VARIABLE, CLIENT_SECRET_VARIABLE, REFRESH_TOKEN_VARIABLE],
)
def test_an_absent_credential_list_fails_the_start_and_names_the_variable(
    tmp_path: Path, variable: str
) -> None:
    plugins_dir, state_path = install_google_workspace(
        tmp_path, "http://127.0.0.1:1"
    )
    loader = PluginLoader(
        directory=plugins_dir,
        state_path=state_path,
        secrets=EnvSecretResolver(
            {
                key: value
                for key, value in {
                    CLIENT_ID_VARIABLE: CLIENT_IDS,
                    CLIENT_SECRET_VARIABLE: CLIENT_SECRETS,
                    REFRESH_TOKEN_VARIABLE: REFRESH_TOKENS,
                }.items()
                if key != variable
            }
        ),
        trace_sink=RecordingTraceSink(),
    )

    with pytest.raises(PluginLoadError) as excinfo:
        _load(loader)

    assert variable in str(excinfo.value)


# ---------------------------------------------------------------------------
# Validation de la configuration : défauts fatals, avant tout appel réseau
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("secrets", "fragment"),
    [
        (
            {CLIENT_ID_VARIABLE: CLIENT_ID_PRO},
            "longueur",
        ),
        (
            {
                CLIENT_ID_VARIABLE: CLIENT_IDS,
                CLIENT_SECRET_VARIABLE: CLIENT_SECRETS,
                REFRESH_TOKEN_VARIABLE: f"{REFRESH_TOKEN_PRO},",
            },
            "vide",
        ),
    ],
    ids=["different-lengths", "empty-entry"],
)
def test_malformed_credential_lists_are_fatal(
    tmp_path: Path, secrets: dict[str, str], fragment: str
) -> None:
    sink = RecordingTraceSink()
    registry, loader = build_plugins(
        tmp_path, sink, "http://127.0.0.1:1", secrets=secrets
    )

    with pytest.raises(PluginLoadError) as excinfo:
        _load(loader, registry)

    message = str(excinfo.value)
    assert fragment in message
    assert all(secret not in message for secret in TOKEN_SECRETS)


def test_an_empty_account_label_is_fatal(tmp_path: Path) -> None:
    sink = RecordingTraceSink()
    registry, loader = build_plugins(
        tmp_path, sink, "http://127.0.0.1:1", accounts="Pro,"
    )

    with pytest.raises(PluginLoadError) as excinfo:
        _load(loader, registry)

    assert "vide" in str(excinfo.value)


def test_two_labels_normalizing_to_the_same_key_are_fatal(tmp_path: Path) -> None:
    sink = RecordingTraceSink()
    registry, loader = build_plugins(
        tmp_path, sink, "http://127.0.0.1:1", accounts="Perso,pérso"
    )

    with pytest.raises(PluginLoadError) as excinfo:
        _load(loader, registry)

    assert "double" in str(excinfo.value)


def test_an_unknown_default_account_is_fatal(tmp_path: Path) -> None:
    sink = RecordingTraceSink()
    registry, loader = build_plugins(
        tmp_path, sink, "http://127.0.0.1:1", default_account="inconnu"
    )

    with pytest.raises(PluginLoadError) as excinfo:
        _load(loader, registry)

    message = str(excinfo.value)
    assert "default_account" in message
    assert "inconnu" in message


# ---------------------------------------------------------------------------
# Démarrage : échange OAuth parallèle, cache en mémoire
# ---------------------------------------------------------------------------


def test_each_account_refresh_token_is_exchanged_at_startup(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                _,
                _,
                _,
            ):
                assert app.plugins.states["google-workspace"] == "activated"

                [row] = app.snapshot()["plugins"]
                assert row["name"] == "google-workspace"
                assert row["state"] == "activated"
                assert row["tools"] == [
                    "calendar_create_event",
                    "calendar_delete_event",
                    "calendar_list_events",
                    "calendar_update_event",
                    "contacts_search",
                    "gmail_read",
                    "gmail_reply",
                    "gmail_search",
                    "gmail_send",
                ]
                assert row["declaration"]["api_version"] == 1

                # Un échange par compte, avec le formulaire OAuth exact.
                assert len(stub.token_requests) == 2
                forms = {
                    request["form"]["refresh_token"][0]: request["form"]
                    for request in stub.token_requests
                }
                assert forms[REFRESH_TOKEN_PRO] == {
                    "grant_type": ["refresh_token"],
                    "client_id": [CLIENT_ID_PRO],
                    "client_secret": [CLIENT_SECRET_PRO],
                    "refresh_token": [REFRESH_TOKEN_PRO],
                }
                assert forms[REFRESH_TOKEN_PERSO] == {
                    "grant_type": ["refresh_token"],
                    "client_id": [CLIENT_ID_PERSO],
                    "client_secret": [CLIENT_SECRET_PERSO],
                    "refresh_token": [REFRESH_TOKEN_PERSO],
                }

                ready = {
                    entry.payload["account"]
                    for entry in sink.of_kind("AccountReady")
                }
                assert ready == {"pro", "perso"}
                assert all(
                    set(entry.payload) == {"plugin", "account"}
                    for entry in sink.of_kind("AccountReady")
                )

                assert all(
                    secret not in _traces_json(sink)
                    for secret in TOKEN_SECRETS
                )

    asyncio.run(scenario())


def test_the_access_token_is_kept_in_memory_and_reused(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                MESSAGES_PATH,
                body=gmail_message_list(),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                app_sink,
            ):
                first = await call_tool(
                    session, "gmail_search", {"query": "facture"}
                )
                second = await call_tool(
                    session, "gmail_search", {"query": "devis"}
                )

                assert first["status"] == "ok"
                assert second["status"] == "ok"
                # Aucun échange supplémentaire : le jeton du démarrage vit
                # en mémoire et n'est pas persisté.
                assert len(stub.token_requests) == 2
                assert not sink.of_kind("TokenRefreshFailed")
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())


def test_a_token_is_refreshed_sixty_seconds_before_it_expires(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
            )
            # Ce jeton expire dans dix secondes : la marge de soixante
            # secondes impose un rafraîchissement avant chaque appel.
            stub.route_token(
                REFRESH_TOKEN_PERSO,
                access_token=ACCESS_TOKEN_PERSO,
                expires_in=10,
            )
            stub.route("GET", MESSAGES_PATH, body=gmail_message_list())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                await call_tool(session, "gmail_search", {"query": "un"})
                await call_tool(session, "gmail_search", {"query": "deux"})

                perso_refreshes = [
                    request
                    for request in stub.token_requests
                    if request["form"]["refresh_token"]
                    == [REFRESH_TOKEN_PERSO]
                ]
                # Démarrage + un rafraîchissement avant chaque recherche.
                assert len(perso_refreshes) == 3

    asyncio.run(scenario())


def test_all_accounts_unavailable_degrades_the_start_then_repairs(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, status=400, error="invalid_grant"
            )
            stub.route_token(
                REFRESH_TOKEN_PERSO, status=400, error="invalid_grant"
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(
                registry=registry, loader=loader, retry_interval=0.1
            ) as (app, live, transport, _):
                # Aucun compte ne répond : Athena démarre quand même, dégradée.
                assert states(sink)[0] == "failed"
                assert app.plugins.states["google-workspace"] == "failed"
                assert app.plugins.tools.specs() == ()
                unavailable = sink.of_kind("AccountUnavailable")
                assert {
                    entry.payload["account"] for entry in unavailable
                } == {"pro", "perso"}
                assert all(
                    entry.payload["reason"] == "invalid_grant"
                    for entry in unavailable
                )

                # Les refresh tokens sont réparés : la retente active le plugin.
                program_tokens(stub)
                stub.route("GET", MESSAGES_PATH, body=gmail_message_list())
                await wait_for_state(app, "google-workspace", "activated")

                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "gmail_search", {"query": "après réparation"}
                    )
                    assert payload["status"] == "ok"
                    assert set(declared(live)) == {
                        "calendar_create_event",
                        "calendar_delete_event",
                        "calendar_list_events",
                        "calendar_update_event",
                        "contacts_search",
                        "gmail_read",
                        "gmail_reply",
                        "gmail_search",
                        "gmail_send",
                    }

    asyncio.run(scenario())


def test_one_unavailable_account_leaves_the_plugin_usable(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
            )
            stub.route_token(
                REFRESH_TOKEN_PERSO, status=400, error="invalid_grant"
            )
            stub.route(
                "GET",
                MESSAGES_PATH,
                body=gmail_message_list(gmail_metadata("m1")),
            )
            stub.route(
                "GET",
                f"{MESSAGES_PATH}/m1",
                body=gmail_metadata("m1"),
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                # Au moins un compte échange : le plugin s'active, l'autre
                # compte reste indisponible et sera retenté à l'appel.
                assert app.plugins.states["google-workspace"] == "activated"
                ready = {
                    entry.payload["account"]
                    for entry in sink.of_kind("AccountReady")
                }
                assert ready == {"pro"}
                [unavailable] = sink.of_kind("AccountUnavailable")
                assert unavailable.payload["account"] == "perso"
                assert unavailable.payload["reason"] == "invalid_grant"

                async with live_session(transport, live) as session:
                    ok = await call_tool(
                        session, "gmail_search", {"query": "x", "account": "pro"}
                    )
                    assert ok["status"] == "ok"
                    assert ok["account"] == "pro"

                    before = len(stub.gmail_requests)
                    failed = await call_tool(
                        session,
                        "gmail_search",
                        {"query": "x", "account": "perso"},
                    )
                    assert failed["status"] == "error"
                    assert "indisponible" in failed["message"]
                    assert "perso" in failed["message"]
                    # Le compte en échec est retenté, mais aucun Gmail n'est lu.
                    assert len(stub.gmail_requests) == before
                    perso_refreshes = [
                        request
                        for request in stub.token_requests
                        if request["form"]["refresh_token"]
                        == [REFRESH_TOKEN_PERSO]
                    ]
                    assert len(perso_refreshes) == 2

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Sélection du compte : nommé, défaut, inconnu
# ---------------------------------------------------------------------------


def test_the_default_account_is_used_when_none_is_named(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                MESSAGES_PATH,
                body=gmail_message_list(gmail_metadata("m1")),
            )
            stub.route(
                "GET", f"{MESSAGES_PATH}/m1", body=gmail_metadata("m1")
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "gmail_search", {"query": "défaut"}
                )

                assert payload["status"] == "ok"
                assert payload["account"] == "perso"
                [request] = stub.requests_for("GET", MESSAGES_PATH)
                assert request["authorization"] == f"Bearer {ACCESS_TOKEN_PERSO}"

    asyncio.run(scenario())


def test_a_named_account_is_used_including_its_human_label(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                MESSAGES_PATH,
                body=gmail_message_list(gmail_metadata("m1")),
            )
            stub.route(
                "GET", f"{MESSAGES_PATH}/m1", body=gmail_metadata("m1")
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_search",
                    {"query": "travail", "account": "Pro"},
                )

                assert payload["status"] == "ok"
                assert payload["account"] == "pro"
                [request] = stub.requests_for("GET", MESSAGES_PATH)
                assert request["authorization"] == f"Bearer {ACCESS_TOKEN_PRO}"

    asyncio.run(scenario())


def test_an_unknown_account_is_a_french_error_without_any_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_search",
                    {"query": "x", "account": "inconnu"},
                )

                assert payload["status"] == "error"
                assert "inconnu" in payload["message"]
                assert "pro" in payload["message"]
                assert "perso" in payload["message"]
                assert stub.gmail_requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tool",
    [
        "contacts_search",
        "gmail_search",
        "gmail_read",
        "gmail_send",
        "gmail_reply",
        "calendar_list_events",
        "calendar_create_event",
        "calendar_update_event",
        "calendar_delete_event",
    ],
)
def test_every_tool_refuses_an_unknown_account_with_the_same_message(
    tmp_path: Path, tool: str
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, tool, {"account": "inconnu"}
                )

                assert payload == {
                    "status": "error",
                    "message": (
                        "Le compte « inconnu » est inconnu. "
                        "Comptes disponibles : pro, perso."
                    ),
                }
                assert stub.gmail_requests == []
                assert stub.calendar_requests == []
                assert stub.requests_for("GET", PEOPLE_CONNECTIONS_PATH) == []
                assert stub.requests_for("GET", PEOPLE_OTHER_PATH) == []

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Contrat de ``gmail_search``
# ---------------------------------------------------------------------------


def test_gmail_search_sends_the_gmail_query_and_returns_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            first = gmail_metadata(
                "m1",
                thread_id="t1",
                sender="Camille <camille@example.test>",
                subject="Déjeuner jeudi",
                date="Thu, 5 Nov 2026 10:00:00 +0100",
                snippet="On se retrouve à midi ?",
            )
            second = gmail_metadata(
                "m2",
                thread_id="t2",
                sender="Bureau <bureau@example.test>",
                subject="Facture de novembre",
                date="Wed, 4 Nov 2026 09:00:00 +0100",
                snippet="La facture est jointe.",
            )
            stub.route(
                "GET",
                MESSAGES_PATH,
                body=gmail_message_list(
                    first, second, next_page_token="page-2"
                ),
            )
            stub.route("GET", f"{MESSAGES_PATH}/m1", body=first)
            stub.route("GET", f"{MESSAGES_PATH}/m2", body=second)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_search",
                    {
                        "query": "from:camille",
                        "max_results": 2,
                        "page_token": "page-1",
                    },
                )

                assert payload == {
                    "status": "ok",
                    "account": "perso",
                    "messages": [
                        {
                            "id": "m1",
                            "thread_id": "t1",
                            "from": "Camille <camille@example.test>",
                            "subject": "Déjeuner jeudi",
                            "date": "Thu, 5 Nov 2026 10:00:00 +0100",
                            "snippet": "On se retrouve à midi ?",
                        },
                        {
                            "id": "m2",
                            "thread_id": "t2",
                            "from": "Bureau <bureau@example.test>",
                            "subject": "Facture de novembre",
                            "date": "Wed, 4 Nov 2026 09:00:00 +0100",
                            "snippet": "La facture est jointe.",
                        },
                    ],
                    "next_page_token": "page-2",
                }

                [listed] = stub.requests_for("GET", MESSAGES_PATH)
                assert listed["query"] == {
                    "q": ["from:camille"],
                    "maxResults": ["2"],
                    "pageToken": ["page-1"],
                }
                assert listed["authorization"] == f"Bearer {ACCESS_TOKEN_PERSO}"

                metadata = [
                    request
                    for request in stub.gmail_requests
                    if request["path"] != MESSAGES_PATH
                ]
                assert [request["path"] for request in metadata] == [
                    f"{MESSAGES_PATH}/m1",
                    f"{MESSAGES_PATH}/m2",
                ]
                for request in metadata:
                    assert request["query"]["format"] == ["metadata"]
                    assert request["query"]["metadataHeaders"] == [
                        "From",
                        "Subject",
                        "Date",
                    ]

    asyncio.run(scenario())


def test_gmail_search_decodes_rfc2047_headers_and_survives_a_malformed_one(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            encoded = gmail_metadata(
                "m1",
                sender=ENCODED_SENDER,
                subject=ENCODED_SUBJECT,
            )
            malformed = gmail_metadata("m2", subject=MALFORMED_SUBJECT)
            stub.route(
                "GET",
                MESSAGES_PATH,
                body=gmail_message_list(encoded, malformed),
            )
            stub.route("GET", f"{MESSAGES_PATH}/m1", body=encoded)
            stub.route("GET", f"{MESSAGES_PATH}/m2", body=malformed)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "gmail_search", {"query": "déjeuner"}
                )

                assert payload["status"] == "ok"
                assert payload["messages"][0]["from"] == (
                    "Camille é <camille@example.test>"
                )
                assert payload["messages"][0]["subject"] == "Déjeuner jeudi"
                # Un en-tête indécodable est rendu tel quel, sans lever.
                assert payload["messages"][1]["subject"] == MALFORMED_SUBJECT

    asyncio.run(scenario())


def test_gmail_search_defaults_to_ten_and_caps_at_twenty_five(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route("GET", MESSAGES_PATH, body=gmail_message_list())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                await call_tool(session, "gmail_search", {"query": "un"})
                await call_tool(
                    session,
                    "gmail_search",
                    {"query": "deux", "max_results": 99},
                )
                await call_tool(
                    session,
                    "gmail_search",
                    {"query": "trois", "max_results": 0},
                )

                values = [
                    request["query"]["maxResults"][0]
                    for request in stub.requests_for("GET", MESSAGES_PATH)
                ]
                assert values == ["10", "25", "1"]

    asyncio.run(scenario())


def test_an_empty_query_is_refused_before_any_gmail_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "gmail_search", {"query": "   "}
                )

                assert payload["status"] == "error"
                assert "vide" in payload["message"]
                assert stub.gmail_requests == []

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Pannes HTTP : un 401 rejoue une fois, les autres statuts ne retentent pas
# ---------------------------------------------------------------------------


def test_a_401_refreshes_once_and_replays_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
            )
            stub.route_token_sequence(
                REFRESH_TOKEN_PERSO,
                [
                    {"access_token": ACCESS_TOKEN_PERSO},
                    {"access_token": "access-token-perso-2"},
                ],
            )
            message = gmail_metadata("m1")
            stub.route_sequence(
                "GET",
                MESSAGES_PATH,
                [
                    {
                        "status": 401,
                        "body": {
                            "error": {"code": 401, "message": "Unauthorized"}
                        },
                    },
                    {"body": gmail_message_list(message)},
                ],
            )
            stub.route("GET", f"{MESSAGES_PATH}/m1", body=message)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "gmail_search", {"query": "après 401"}
                )

                assert payload["status"] == "ok"
                assert [m["id"] for m in payload["messages"]] == ["m1"]

                listed = stub.requests_for("GET", MESSAGES_PATH)
                assert [request["authorization"] for request in listed] == [
                    f"Bearer {ACCESS_TOKEN_PERSO}",
                    "Bearer access-token-perso-2",
                ]
                perso_refreshes = [
                    request
                    for request in stub.token_requests
                    if request["form"]["refresh_token"]
                    == [REFRESH_TOKEN_PERSO]
                ]
                # Démarrage + le seul rafraîchissement du 401.
                assert len(perso_refreshes) == 2

    asyncio.run(scenario())


def test_a_second_401_is_not_retried_again(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
            )
            stub.route_token_sequence(
                REFRESH_TOKEN_PERSO,
                [
                    {"access_token": ACCESS_TOKEN_PERSO},
                    {"access_token": "access-token-perso-2"},
                ],
            )
            stub.route_sequence(
                "GET",
                MESSAGES_PATH,
                [
                    {
                        "status": 401,
                        "body": {"error": {"code": 401}},
                    },
                    {
                        "status": 401,
                        "body": {"error": {"code": 401}},
                    },
                ],
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "gmail_search", {"query": "toujours 401"}
                )

                assert payload["status"] == "error"
                assert "refusé" in payload["message"]
                assert len(stub.requests_for("GET", MESSAGES_PATH)) == 2
                perso_refreshes = [
                    request
                    for request in stub.token_requests
                    if request["form"]["refresh_token"]
                    == [REFRESH_TOKEN_PERSO]
                ]
                assert len(perso_refreshes) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (403, "refusé"),
        (429, "trop de requêtes"),
        (500, "indisponible"),
        (503, "réponse 503"),
    ],
)
def test_a_non_401_status_becomes_a_french_sentence_without_retry(
    tmp_path: Path, status: int, fragment: str
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                MESSAGES_PATH,
                status=status,
                body={"error": {"code": status}},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                app_sink,
            ):
                payload = await call_tool(
                    session, "gmail_search", {"query": "refusée"}
                )

                assert payload["status"] == "error"
                assert payload["message"].startswith(
                    "La recherche Gmail n'a pas abouti"
                )
                assert fragment in payload["message"]
                # Aucun rejeu et aucun rafraîchissement supplémentaire.
                assert len(stub.requests_for("GET", MESSAGES_PATH)) == 1
                assert len(stub.token_requests) == 2
                assert sink.of_kind("ToolFailed")[-1].payload["status"] == (
                    f"HTTP {status}"
                )
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Instruction, Console, Traces : aucune fuite
# ---------------------------------------------------------------------------


def test_the_tool_instruction_and_console_row_are_exact(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                app,
                live,
                _,
                _,
                _,
            ):
                tools = declared(live)
                assert set(tools) == {
                    "calendar_create_event",
                    "calendar_delete_event",
                    "calendar_list_events",
                    "calendar_update_event",
                    "contacts_search",
                    "gmail_read",
                    "gmail_reply",
                    "gmail_search",
                    "gmail_send",
                }
                assert (
                    dict(tools["gmail_search"].parameters_json_schema)
                    == GMAIL_SEARCH_PARAMETERS
                )
                assert (
                    tools["gmail_search"].description
                    == GMAIL_SEARCH_DESCRIPTION
                )
                assert (
                    dict(tools["gmail_read"].parameters_json_schema)
                    == GMAIL_READ_PARAMETERS
                )
                assert (
                    tools["gmail_read"].description == GMAIL_READ_DESCRIPTION
                )

                # Les libellés normalisés sont les noms montrés à Gemini.
                assert INSTRUCTION in live.instruction()

                [row] = app.snapshot()["plugins"]
                assert row["tools"] == [
                    "calendar_create_event",
                    "calendar_delete_event",
                    "calendar_list_events",
                    "calendar_update_event",
                    "contacts_search",
                    "gmail_read",
                    "gmail_reply",
                    "gmail_search",
                    "gmail_send",
                ]

    asyncio.run(scenario())


def test_traces_never_carry_a_secret_query_address_subject_or_url(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            message = gmail_metadata(
                "m1",
                sender="camille@example.test",
                subject="Objet confidentiel",
                snippet="Extrait confidentiel",
            )
            stub.route(
                "GET",
                MESSAGES_PATH,
                body=gmail_message_list(message),
            )
            stub.route("GET", f"{MESSAGES_PATH}/m1", body=message)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                ok = await call_tool(
                    session,
                    "gmail_search",
                    {"query": "requête confidentielle"},
                )
                assert ok["status"] == "ok"

                [called] = sink.of_kind("ToolCalled")
                assert called.payload["tool"] == "gmail_search"
                assert called.payload["account"] == "perso"
                assert called.payload["status"] == "ok"
                assert called.payload["result_count"] == 1
                assert isinstance(called.payload["duration_ms"], (int, float))
                assert set(called.payload) == {
                    "plugin",
                    "tool",
                    "account",
                    "duration_ms",
                    "status",
                    "result_count",
                }

                rendered = _traces_json(sink)
                for forbidden in (
                    *TOKEN_SECRETS,
                    ACCESS_TOKEN_PERSO,
                    "camille@example.test",
                    "Objet confidentiel",
                    "Extrait confidentiel",
                    "requête confidentielle",
                    MESSAGES_PATH,
                ):
                    assert forbidden not in rendered

                # La Trace de démarrage ne porte que le libellé du compte.
                assert all(
                    set(entry.payload) == {"plugin", "account"}
                    for entry in sink.of_kind("AccountReady")
                )

    asyncio.run(scenario())


def test_an_error_traces_tool_called_and_tool_failed_without_content(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                MESSAGES_PATH,
                status=403,
                body={"error": {"code": 403}},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_search",
                    {"query": "requête secrète"},
                )
                assert payload["status"] == "error"

                [called] = sink.of_kind("ToolCalled")
                assert called.payload["status"] == "error"
                assert "result_count" not in called.payload
                [failed] = sink.of_kind("ToolFailed")
                assert failed.payload["tool"] == "gmail_search"
                assert failed.payload["account"] == "perso"
                assert failed.payload["status"] == "HTTP 403"
                assert "requête secrète" not in _traces_json(sink)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Contrat de ``gmail_read`` : message, fil, corps, pièces jointes
# ---------------------------------------------------------------------------


def test_gmail_read_fetches_a_message_and_returns_its_text(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            message = gmail_read_message(
                "m1",
                thread_id="t1",
                sender="Camille <camille@example.test>",
                recipients="Cooper <cooper@example.test>",
                cc="Bureau <bureau@example.test>",
                subject="Déjeuner jeudi",
                date="Thu, 5 Nov 2026 10:00:00 +0100",
                plain="On se retrouve à midi ?\nÀ bientôt.",
                attachments=("menu.pdf",),
            )
            stub.route("GET", f"{MESSAGES_PATH}/m1", body=message)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "gmail_read", {"message_id": "m1"}
                )

                assert payload == {
                    "status": "ok",
                    "account": "perso",
                    "thread_id": "t1",
                    "messages": [
                        {
                            "id": "m1",
                            "from": "Camille <camille@example.test>",
                            "to": "Cooper <cooper@example.test>",
                            "cc": "Bureau <bureau@example.test>",
                            "subject": "Déjeuner jeudi",
                            "date": "Thu, 5 Nov 2026 10:00:00 +0100",
                            "body": "On se retrouve à midi ?\nÀ bientôt.",
                            "truncated": False,
                            "attachments": ["menu.pdf"],
                        }
                    ],
                }

                # Une seule requête : la pièce jointe n'est jamais téléchargée.
                [request] = stub.gmail_requests
                assert request["path"] == f"{MESSAGES_PATH}/m1"
                assert request["query"] == {"format": ["full"]}
                assert request["authorization"] == f"Bearer {ACCESS_TOKEN_PERSO}"

    asyncio.run(scenario())


def test_gmail_read_decodes_rfc2047_headers_on_a_message_and_a_thread(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            message = gmail_read_message(
                "m1",
                thread_id="t1",
                sender=ENCODED_SENDER,
                subject=ENCODED_SUBJECT,
            )
            threaded = gmail_read_message(
                "m2",
                thread_id="t2",
                sender=ENCODED_SENDER,
                subject=ENCODED_SUBJECT,
            )
            stub.route("GET", f"{MESSAGES_PATH}/m1", body=message)
            stub.route(
                "GET",
                f"{THREADS_PATH}/t2",
                body=gmail_read_thread("t2", threaded),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                single = await call_tool(
                    session, "gmail_read", {"message_id": "m1"}
                )
                thread = await call_tool(
                    session, "gmail_read", {"thread_id": "t2"}
                )

                for payload in (single, thread):
                    assert payload["status"] == "ok"
                    [rendered] = payload["messages"]
                    assert rendered["from"] == (
                        "Camille é <camille@example.test>"
                    )
                    assert rendered["subject"] == "Déjeuner jeudi"

    asyncio.run(scenario())


def test_gmail_read_reads_a_thread_bounded_by_max_messages(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            thread = gmail_read_thread(
                "t1",
                gmail_read_message(
                    "m1",
                    thread_id="t1",
                    subject="Déjeuner jeudi",
                    plain="Premier message.",
                ),
                gmail_read_message(
                    "m2",
                    thread_id="t1",
                    subject="Re: Déjeuner jeudi",
                    plain="Deuxième message.",
                ),
                gmail_read_message(
                    "m3",
                    thread_id="t1",
                    subject="Re: Déjeuner jeudi",
                    plain="Troisième message.",
                ),
            )
            stub.route("GET", f"{THREADS_PATH}/t1", body=thread)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_read",
                    {"thread_id": "t1", "max_messages": 2},
                )

                assert payload["status"] == "ok"
                assert payload["account"] == "perso"
                assert payload["thread_id"] == "t1"
                assert [message["id"] for message in payload["messages"]] == [
                    "m1",
                    "m2",
                ]
                # Le fil contenait plus de messages que la borne : annoncé.
                assert payload["truncated"] is True

                # Un seul GET de fil : aucun message n'est relu séparément.
                [request] = stub.gmail_requests
                assert request["path"] == f"{THREADS_PATH}/t1"
                assert request["query"] == {"format": ["full"]}

    asyncio.run(scenario())


def test_gmail_read_defaults_a_thread_to_ten_and_caps_at_twenty(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            thread = gmail_read_thread(
                "t1",
                *(
                    gmail_read_message(
                        f"m{index}", thread_id="t1", plain=f"Message {index}."
                    )
                    for index in range(1, 26)
                ),
            )
            stub.route("GET", f"{THREADS_PATH}/t1", body=thread)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                default = await call_tool(
                    session, "gmail_read", {"thread_id": "t1"}
                )
                capped = await call_tool(
                    session,
                    "gmail_read",
                    {"thread_id": "t1", "max_messages": 99},
                )
                floored = await call_tool(
                    session,
                    "gmail_read",
                    {"thread_id": "t1", "max_messages": 0},
                )

                assert len(default["messages"]) == 10
                assert [message["id"] for message in default["messages"]][0] == "m1"
                assert len(capped["messages"]) == 20
                assert len(floored["messages"]) == 1

    asyncio.run(scenario())


def test_gmail_read_falls_back_to_cleaned_html_and_prefers_plain_text(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                f"{MESSAGES_PATH}/html",
                body=gmail_read_message(
                    "html",
                    plain=None,
                    html=(
                        "<html><body><p>Bonjour&nbsp;Camille,</p>"
                        "<p>Voici le <b>compte rendu</b>.</p>"
                        "<script>alert('secret')</script></body></html>"
                    ),
                ),
            )
            stub.route(
                "GET",
                f"{MESSAGES_PATH}/both",
                body=gmail_read_message(
                    "both",
                    plain="La version texte.",
                    html="<p>La version HTML.</p>",
                ),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                [html_only] = (await call_tool(
                    session, "gmail_read", {"message_id": "html"}
                ))["messages"]
                [both] = (await call_tool(
                    session, "gmail_read", {"message_id": "both"}
                ))["messages"]

                assert html_only["body"] == (
                    "Bonjour Camille,\nVoici le compte rendu."
                )
                assert "secret" not in html_only["body"]
                assert html_only["truncated"] is False
                # Le texte brut l'emporte quand les deux existent.
                assert both["body"] == "La version texte."

    asyncio.run(scenario())


def test_gmail_read_truncates_a_long_body_at_six_thousand_characters(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            long_body = "x" * 6500 + "FIN"
            stub.route(
                "GET",
                f"{MESSAGES_PATH}/long",
                body=gmail_read_message("long", plain=long_body),
            )
            stub.route(
                "GET",
                f"{MESSAGES_PATH}/exact",
                body=gmail_read_message("exact", plain="y" * 6000),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                truncated = await call_tool(
                    session, "gmail_read", {"message_id": "long"}
                )
                exact = await call_tool(
                    session, "gmail_read", {"message_id": "exact"}
                )

                [message] = truncated["messages"]
                assert message["body"] == long_body[:6000]
                assert len(message["body"]) == 6000
                assert message["truncated"] is True
                # La coupe est aussi annoncée au niveau de la réponse.
                assert truncated["truncated"] is True

                [whole] = exact["messages"]
                assert whole["body"] == "y" * 6000
                assert whole["truncated"] is False
                assert "truncated" not in exact

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    [
        {"message_id": "m1", "thread_id": "t1"},
        {},
        {"message_id": "   "},
    ],
    ids=["both", "neither", "blank"],
)
def test_gmail_read_refuses_a_missing_or_doubled_identifier_before_any_call(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                payload = await call_tool(session, "gmail_read", arguments)

                assert payload["status"] == "error"
                assert payload["message"].startswith(
                    "La lecture du message Gmail n'a pas abouti"
                )
                assert stub.gmail_requests == []
                failed = sink.of_kind("ToolFailed")[-1]
                assert failed.payload["tool"] == "gmail_read"
                assert failed.payload["status"] == "invalid_identifier"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (404, "introuvable"),
        (500, "indisponible"),
        (403, "refusé"),
    ],
)
def test_gmail_read_turns_an_http_status_into_a_french_sentence(
    tmp_path: Path, status: int, fragment: str
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                f"{MESSAGES_PATH}/m1",
                status=status,
                body={"error": {"code": status}},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                app_sink,
            ):
                payload = await call_tool(
                    session, "gmail_read", {"message_id": "m1"}
                )

                assert payload["status"] == "error"
                assert payload["message"].startswith(
                    "La lecture du message Gmail n'a pas abouti"
                )
                assert fragment in payload["message"]
                # Aucun rejeu, aucun rafraîchissement supplémentaire.
                assert len(stub.requests_for("GET", f"{MESSAGES_PATH}/m1")) == 1
                assert len(stub.token_requests) == 2
                assert sink.of_kind("ToolFailed")[-1].payload["status"] == (
                    f"HTTP {status}"
                )
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())


def test_gmail_read_traces_carry_no_identifier_address_subject_or_body(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            message = gmail_read_message(
                "m-secret-1",
                thread_id="t-secret-1",
                sender="secret@example.test",
                subject="Objet très confidentiel",
                plain="Le corps secret du message.",
                attachments=("facture-secrete.pdf",),
            )
            stub.route(
                "GET", f"{MESSAGES_PATH}/m-secret-1", body=message
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                payload = await call_tool(
                    session, "gmail_read", {"message_id": "m-secret-1"}
                )
                assert payload["status"] == "ok"

                [called] = sink.of_kind("ToolCalled")
                assert called.payload == {
                    "plugin": "google-workspace",
                    "tool": "gmail_read",
                    "account": "perso",
                    "duration_ms": called.payload["duration_ms"],
                    "status": "ok",
                    "result_count": 1,
                }
                assert isinstance(called.payload["duration_ms"], (int, float))

                rendered = _traces_json(sink)
                for forbidden in (
                    *TOKEN_SECRETS,
                    ACCESS_TOKEN_PERSO,
                    "m-secret-1",
                    "t-secret-1",
                    "secret@example.test",
                    "Objet très confidentiel",
                    "Le corps secret du message.",
                    "facture-secrete.pdf",
                    MESSAGES_PATH,
                ):
                    assert forbidden not in rendered

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Contrat de ``calendar_list_events``
# ---------------------------------------------------------------------------


def test_calendar_list_events_is_declared_with_its_schema(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                live,
                _,
                _,
                _,
            ):
                tools = declared(live)
                declaration = tools["calendar_list_events"]
                assert (
                    dict(declaration.parameters_json_schema)
                    == CALENDAR_LIST_EVENTS_PARAMETERS
                )
                assert declaration.parameters_json_schema["properties"][
                    "page_token"
                ] == {
                    "type": "string",
                    "description": "Jeton de page rendu par l'appel précédent.",
                }
                assert (
                    declaration.description
                    == CALENDAR_LIST_EVENTS_DESCRIPTION
                )

    asyncio.run(scenario())


def test_calendar_list_events_sends_the_window_and_renders_events(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            timed = calendar_event(
                "e1",
                summary="Déjeuner jeudi",
                start="2026-11-12T12:00:00+01:00",
                end="2026-11-12T13:30:00+01:00",
                location="Cantine",
                attendees=[
                    ("camille@example.test", "accepted"),
                    ("bureau@example.test", "declined"),
                ],
                status="confirmed",
                html_link="https://calendar.google.com/calendar/event?eid=e1",
            )
            whole_day = calendar_event(
                "e2",
                summary="Congé",
                start="2026-11-13",
                end="2026-11-14",
                all_day=True,
                location="",
                status="tentative",
                html_link="https://calendar.google.com/calendar/event?eid=e2",
            )
            stub.route(
                "GET",
                CALENDAR_PRIMARY_PATH,
                body=calendar_event_list(
                    timed, whole_day, next_page_token="page-2"
                ),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_list_events",
                    {
                        "time_min": "2026-11-12T00:00:00+01:00",
                        "time_max": "2026-11-19T00:00:00+01:00",
                        "max_results": 2,
                        "page_token": "page-1",
                        "query": "déjeuner",
                    },
                )

                assert payload == {
                    "status": "ok",
                    "account": "perso",
                    "calendar_id": "primary",
                    "events": [
                        {
                            "id": "e1",
                            "summary": "Déjeuner jeudi",
                            "start": "2026-11-12T12:00:00+01:00",
                            "end": "2026-11-12T13:30:00+01:00",
                            "all_day": False,
                            "location": "Cantine",
                            "attendees": [
                                {
                                    "email": "camille@example.test",
                                    "response_status": "accepted",
                                },
                                {
                                    "email": "bureau@example.test",
                                    "response_status": "declined",
                                },
                            ],
                            "status": "confirmed",
                            "html_link": (
                                "https://calendar.google.com/calendar/event?eid=e1"
                            ),
                        },
                        {
                            "id": "e2",
                            "summary": "Congé",
                            "start": "2026-11-13",
                            "end": "2026-11-14",
                            "all_day": True,
                            "location": "",
                            "attendees": [],
                            "status": "tentative",
                            "html_link": (
                                "https://calendar.google.com/calendar/event?eid=e2"
                            ),
                        },
                    ],
                    "next_page_token": "page-2",
                }

                [listed] = stub.requests_for("GET", CALENDAR_PRIMARY_PATH)
                assert listed["query"] == {
                    "timeMin": ["2026-11-12T00:00:00+01:00"],
                    "timeMax": ["2026-11-19T00:00:00+01:00"],
                    "maxResults": ["2"],
                    "pageToken": ["page-1"],
                    "singleEvents": ["true"],
                    "orderBy": ["startTime"],
                    "q": ["déjeuner"],
                }
                assert listed["authorization"] == f"Bearer {ACCESS_TOKEN_PERSO}"

    asyncio.run(scenario())


def test_calendar_list_events_defaults_to_now_and_seven_days(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET", CALENDAR_PRIMARY_PATH, body=calendar_event_list()
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "calendar_list_events", {}
                )

                assert payload == {
                    "status": "ok",
                    "account": "perso",
                    "calendar_id": "primary",
                    "events": [],
                }

                [listed] = stub.requests_for("GET", CALENDAR_PRIMARY_PATH)
                query = listed["query"]
                assert set(query) == {
                    "timeMin",
                    "timeMax",
                    "maxResults",
                    "singleEvents",
                    "orderBy",
                }
                assert query["maxResults"] == ["10"]
                assert query["singleEvents"] == ["true"]
                assert query["orderBy"] == ["startTime"]

                time_min = datetime.fromisoformat(query["timeMin"][0])
                time_max = datetime.fromisoformat(query["timeMax"][0])
                assert time_min.tzinfo is not None
                assert time_max.tzinfo is not None
                assert time_max - time_min == timedelta(days=7)
                now = datetime.now().astimezone()
                assert abs((now - time_min).total_seconds()) < 60

                # Les bornes de ``max_results`` sont 1 et 50.
                await call_tool(
                    session,
                    "calendar_list_events",
                    {"max_results": 99},
                )
                await call_tool(
                    session,
                    "calendar_list_events",
                    {"max_results": 0},
                )
                values = [
                    request["query"]["maxResults"][0]
                    for request in stub.requests_for(
                        "GET", CALENDAR_PRIMARY_PATH
                    )
                ]
                assert values == ["10", "50", "1"]

    asyncio.run(scenario())


def test_the_calendar_id_is_percent_encoded_in_the_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        calendar_id = "famille#holiday@group.calendar.google.com"
        decoded_path = f"{CALENDAR_EVENTS_BASE}/{calendar_id}/events"
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET", decoded_path, body=calendar_event_list()
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_list_events",
                    {"calendar_id": calendar_id},
                )

                assert payload["status"] == "ok"
                assert payload["calendar_id"] == calendar_id
                [listed] = stub.calendar_requests
                assert listed["raw_path"] == (
                    f"{CALENDAR_EVENTS_BASE}/"
                    "famille%23holiday%40group.calendar.google.com/events"
                )
                assert listed["path"] == decoded_path

    asyncio.run(scenario())


def test_a_calendar_401_refreshes_once_and_replays_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
            )
            stub.route_token_sequence(
                REFRESH_TOKEN_PERSO,
                [
                    {"access_token": ACCESS_TOKEN_PERSO},
                    {"access_token": "access-token-perso-2"},
                ],
            )
            stub.route_sequence(
                "GET",
                CALENDAR_PRIMARY_PATH,
                [
                    {
                        "status": 401,
                        "body": {"error": {"code": 401}},
                    },
                    {"body": calendar_event_list(calendar_event("e1"))},
                ],
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "calendar_list_events", {}
                )

                assert payload["status"] == "ok"
                assert [event["id"] for event in payload["events"]] == ["e1"]

                listed = stub.requests_for("GET", CALENDAR_PRIMARY_PATH)
                assert [request["authorization"] for request in listed] == [
                    f"Bearer {ACCESS_TOKEN_PERSO}",
                    "Bearer access-token-perso-2",
                ]
                perso_refreshes = [
                    request
                    for request in stub.token_requests
                    if request["form"]["refresh_token"]
                    == [REFRESH_TOKEN_PERSO]
                ]
                # Démarrage + le seul rafraîchissement du 401.
                assert len(perso_refreshes) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (400, "période"),
        (403, "refusé"),
        (404, "introuvable"),
        (429, "trop de requêtes"),
        (500, "indisponible"),
        (503, "réponse 503"),
    ],
)
def test_a_failing_calendar_call_is_a_french_sentence_without_retry(
    tmp_path: Path, status: int, fragment: str
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                CALENDAR_PRIMARY_PATH,
                status=status,
                body={"error": {"code": status}},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                app_sink,
            ):
                payload = await call_tool(
                    session, "calendar_list_events", {}
                )

                assert payload["status"] == "error"
                assert payload["message"].startswith(
                    "La lecture de l'agenda n'a pas abouti"
                )
                assert fragment in payload["message"]
                # Aucun rejeu et aucun rafraîchissement supplémentaire.
                assert (
                    len(stub.requests_for("GET", CALENDAR_PRIMARY_PATH)) == 1
                )
                assert len(stub.token_requests) == 2
                assert sink.of_kind("ToolFailed")[-1].payload["status"] == (
                    f"HTTP {status}"
                )
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())


def test_an_unreadable_calendar_body_is_a_french_sentence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                CALENDAR_PRIMARY_PATH,
                raw_body=b"pas du JSON",
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "calendar_list_events", {}
                )

                assert payload["status"] == "error"
                assert payload["message"].startswith(
                    "La lecture de l'agenda n'a pas abouti"
                )
                assert "illisible" in payload["message"]

    asyncio.run(scenario())


def test_a_minimal_calendar_event_renders_empty_fields_and_is_not_all_day(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                CALENDAR_PRIMARY_PATH,
                body={"items": [{"id": "e3"}]},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "calendar_list_events", {}
                )

                assert payload["events"] == [
                    {
                        "id": "e3",
                        "summary": "",
                        "start": "",
                        "end": "",
                        "all_day": False,
                        "location": "",
                        "attendees": [],
                        "status": "",
                        "html_link": "",
                    }
                ]

    asyncio.run(scenario())


def test_an_unknown_account_is_refused_before_any_calendar_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_list_events",
                    {"account": "inconnu"},
                )

                assert payload["status"] == "error"
                assert "inconnu" in payload["message"]
                assert "pro" in payload["message"]
                assert "perso" in payload["message"]
                assert stub.calendar_requests == []

    asyncio.run(scenario())


def test_calendar_traces_never_carry_query_address_summary_or_url(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            event = calendar_event(
                "e1",
                summary="Réunion confidentielle",
                location="Salle secrète",
                attendees=[("camille@example.test", "accepted")],
            )
            stub.route(
                "GET",
                CALENDAR_PRIMARY_PATH,
                body=calendar_event_list(event),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                ok = await call_tool(
                    session,
                    "calendar_list_events",
                    {"query": "agenda confidentiel"},
                )
                assert ok["status"] == "ok"

                [called] = sink.of_kind("ToolCalled")
                assert called.payload["tool"] == "calendar_list_events"
                assert called.payload["account"] == "perso"
                assert called.payload["status"] == "ok"
                assert called.payload["result_count"] == 1
                assert isinstance(called.payload["duration_ms"], (int, float))
                assert set(called.payload) == {
                    "plugin",
                    "tool",
                    "account",
                    "duration_ms",
                    "status",
                    "result_count",
                }

                rendered = _traces_json(sink)
                for forbidden in (
                    *TOKEN_SECRETS,
                    ACCESS_TOKEN_PERSO,
                    "camille@example.test",
                    "Réunion confidentielle",
                    "Salle secrète",
                    "agenda confidentiel",
                    CALENDAR_EVENTS_BASE,
                ):
                    assert forbidden not in rendered

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Contrat de ``calendar_create_event``, ``calendar_update_event`` et
# ``calendar_delete_event`` (#140)
# ---------------------------------------------------------------------------


CALENDAR_WRITE_TOOLS = {
    "calendar_create_event": (
        CALENDAR_CREATE_EVENT_DESCRIPTION,
        CALENDAR_CREATE_EVENT_PARAMETERS,
    ),
    "calendar_update_event": (
        CALENDAR_UPDATE_EVENT_DESCRIPTION,
        CALENDAR_UPDATE_EVENT_PARAMETERS,
    ),
    "calendar_delete_event": (
        CALENDAR_DELETE_EVENT_DESCRIPTION,
        CALENDAR_DELETE_EVENT_PARAMETERS,
    ),
}


def test_calendar_write_tools_are_declared_with_their_schemas(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                live,
                _,
                _,
                _,
            ):
                tools = declared(live)
                for name, (description, parameters) in (
                    CALENDAR_WRITE_TOOLS.items()
                ):
                    assert (
                        dict(tools[name].parameters_json_schema)
                        == parameters
                    )
                    assert tools[name].description == description

    asyncio.run(scenario())


def test_calendar_create_event_posts_the_event_without_notifying(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            html_link = "https://calendar.google.com/calendar/event?eid=e9"
            stub.route(
                "POST",
                CALENDAR_PRIMARY_PATH,
                body={"id": "e9", "htmlLink": html_link},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_create_event",
                    {
                        "summary": "Réunion budget",
                        "start": "2026-11-12T09:00:00+01:00",
                        "end": "2026-11-12T10:00:00+01:00",
                        "description": "Préparer le budget 2027",
                        "location": "Salle 2",
                        "attendees": [
                            "camille@example.test",
                            "bureau@example.test",
                        ],
                    },
                )

                assert payload == {
                    "status": "ok",
                    "event_id": "e9",
                    "html_link": html_link,
                }

                [created] = stub.requests_for(
                    "POST", CALENDAR_PRIMARY_PATH
                )
                assert created["authorization"] == (
                    f"Bearer {ACCESS_TOKEN_PERSO}"
                )
                assert created["query"] == {"sendUpdates": ["none"]}
                assert created["body"] == {
                    "summary": "Réunion budget",
                    "start": {"dateTime": "2026-11-12T09:00:00+01:00"},
                    "end": {"dateTime": "2026-11-12T10:00:00+01:00"},
                    "description": "Préparer le budget 2027",
                    "location": "Salle 2",
                    "attendees": [
                        {"email": "camille@example.test"},
                        {"email": "bureau@example.test"},
                    ],
                }

    asyncio.run(scenario())


def test_an_all_day_event_sends_dates_with_an_exclusive_end_and_notifies(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "POST",
                CALENDAR_PRIMARY_PATH,
                body={"id": "e10", "htmlLink": "https://example.test/e10"},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_create_event",
                    {
                        "summary": "Congé",
                        "start": "2026-11-13",
                        "end": "2026-11-13",
                        "all_day": True,
                        "notify_attendees": True,
                    },
                )

                assert payload["status"] == "ok"
                [created] = stub.requests_for(
                    "POST", CALENDAR_PRIMARY_PATH
                )
                assert created["query"] == {"sendUpdates": ["all"]}
                assert created["body"] == {
                    "summary": "Congé",
                    "start": {"date": "2026-11-13"},
                    "end": {"date": "2026-11-14"},
                }

    asyncio.run(scenario())


def test_a_date_only_bound_is_all_day_even_without_the_flag(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "POST",
                CALENDAR_PRIMARY_PATH,
                body={"id": "e11", "htmlLink": "https://example.test/e11"},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_create_event",
                    {
                        "summary": "Congé",
                        "start": "2026-11-13",
                        "end": "2026-11-13",
                    },
                )

                assert payload["status"] == "ok"
                [created] = stub.requests_for(
                    "POST", CALENDAR_PRIMARY_PATH
                )
                assert created["body"] == {
                    "summary": "Congé",
                    "start": {"date": "2026-11-13"},
                    "end": {"date": "2026-11-14"},
                }

    asyncio.run(scenario())


def test_a_date_only_update_is_all_day_even_without_the_flag(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        event_path = f"{CALENDAR_PRIMARY_PATH}/e2"
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route("PATCH", event_path, body={"id": "e2"})
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_update_event",
                    {
                        "event_id": "e2",
                        "start": "2026-11-20",
                        "end": "2026-11-21",
                    },
                )

                assert payload["status"] == "ok"
                [patched] = stub.requests_for("PATCH", event_path)
                assert patched["body"] == {
                    "start": {"date": "2026-11-20"},
                    "end": {"date": "2026-11-22"},
                }

    asyncio.run(scenario())


def test_an_all_day_event_still_requires_date_only_bounds(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_create_event",
                    {
                        "summary": "Congé",
                        "start": "2026-11-13T09:00:00+01:00",
                        "end": "2026-11-13T10:00:00+01:00",
                        "all_day": True,
                    },
                )

                assert payload["status"] == "error"
                assert "journée entière est invalide" in payload["message"]
                assert "AAAA-MM-JJ" in payload["message"]
                assert stub.calendar_requests == []

    asyncio.run(scenario())


def test_calendar_update_event_patches_only_the_fields_provided(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        event_path = f"{CALENDAR_PRIMARY_PATH}/e1"
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "PATCH",
                event_path,
                body={
                    "id": "e1",
                    "htmlLink": "https://example.test/e1",
                },
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_update_event",
                    {
                        "event_id": "e1",
                        "summary": "Nouveau titre",
                        "location": "",
                        "attendees": ["camille@example.test"],
                    },
                )

                assert payload == {
                    "status": "ok",
                    "event_id": "e1",
                    "html_link": "https://example.test/e1",
                }
                [patched] = stub.requests_for("PATCH", event_path)
                assert patched["query"] == {"sendUpdates": ["none"]}
                assert patched["body"] == {
                    "summary": "Nouveau titre",
                    "location": "",
                    "attendees": [{"email": "camille@example.test"}],
                }

    asyncio.run(scenario())


def test_calendar_update_event_moves_an_all_day_event_notifying_attendees(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        event_path = f"{CALENDAR_PRIMARY_PATH}/e2"
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route("PATCH", event_path, body={"id": "e2"})
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_update_event",
                    {
                        "event_id": "e2",
                        "start": "2026-11-20",
                        "end": "2026-11-21",
                        "all_day": True,
                        "notify_attendees": True,
                    },
                )

                assert payload == {
                    "status": "ok",
                    "event_id": "e2",
                    "html_link": "",
                }
                [patched] = stub.requests_for("PATCH", event_path)
                assert patched["query"] == {"sendUpdates": ["all"]}
                assert patched["body"] == {
                    "start": {"date": "2026-11-20"},
                    "end": {"date": "2026-11-22"},
                }

    asyncio.run(scenario())


def test_calendar_delete_event_refuses_without_confirmation_and_calls_nothing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                for arguments in (
                    {"event_id": "e1"},
                    {"event_id": "e1", "confirm": False},
                ):
                    payload = await call_tool(
                        session, "calendar_delete_event", arguments
                    )
                    assert payload["status"] == "error"
                    assert payload["message"].startswith(
                        "La suppression de l'événement d'agenda n'a pas "
                        "abouti"
                    )
                    assert "confirmation" in payload["message"]

                assert stub.calendar_requests == []

    asyncio.run(scenario())


def test_calendar_delete_event_sends_a_delete_after_confirmation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        event_path = f"{CALENDAR_PRIMARY_PATH}/e1"
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route("DELETE", event_path, status=204)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_delete_event",
                    {
                        "event_id": "e1",
                        "confirm": True,
                        "notify_attendees": True,
                    },
                )

                assert payload == {"status": "ok", "event_id": "e1"}
                [deleted] = stub.requests_for("DELETE", event_path)
                assert deleted["query"] == {"sendUpdates": ["all"]}
                assert deleted["authorization"] == (
                    f"Bearer {ACCESS_TOKEN_PERSO}"
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [404, 410])
def test_a_missing_event_on_delete_is_an_idempotent_success(
    tmp_path: Path, status: int
) -> None:
    async def scenario() -> None:
        event_path = f"{CALENDAR_PRIMARY_PATH}/e1"
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "DELETE",
                event_path,
                status=status,
                body={"error": {"code": status}},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_delete_event",
                    {"event_id": "e1", "confirm": True},
                )

                assert payload == {"status": "ok", "event_id": "e1"}
                [deleted] = stub.requests_for("DELETE", event_path)
                assert deleted["query"] == {"sendUpdates": ["none"]}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("tool", "method", "path", "arguments", "prefix"),
    [
        (
            "calendar_create_event",
            "POST",
            CALENDAR_PRIMARY_PATH,
            {
                "summary": "Réunion",
                "start": "2026-11-12T09:00:00+01:00",
                "end": "2026-11-12T10:00:00+01:00",
            },
            "La création de l'événement d'agenda n'a pas abouti",
        ),
        (
            "calendar_update_event",
            "PATCH",
            f"{CALENDAR_PRIMARY_PATH}/e1",
            {"event_id": "e1", "summary": "Réunion"},
            "La modification de l'événement d'agenda n'a pas abouti",
        ),
        (
            "calendar_delete_event",
            "DELETE",
            f"{CALENDAR_PRIMARY_PATH}/e1",
            {"event_id": "e1", "confirm": True},
            "La suppression de l'événement d'agenda n'a pas abouti",
        ),
    ],
)
@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (400, "refusée"),
        (403, "refusé"),
        (429, "trop de requêtes"),
        (500, "indisponible"),
        (503, "réponse 503"),
    ],
)
def test_a_failing_calendar_write_is_a_french_sentence_without_retry(
    tmp_path: Path,
    tool: str,
    method: str,
    path: str,
    arguments: dict[str, Any],
    prefix: str,
    status: int,
    fragment: str,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                method,
                path,
                status=status,
                body={"error": {"code": status}},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                app_sink,
            ):
                payload = await call_tool(session, tool, arguments)

                assert payload["status"] == "error"
                assert payload["message"].startswith(prefix)
                assert fragment in payload["message"]
                # Un seul appel, pas de rejeu ni de rafraîchissement ajouté.
                assert len(stub.requests_for(method, path)) == 1
                assert len(stub.token_requests) == 2
                assert sink.of_kind("ToolFailed")[-1].payload["status"] == (
                    f"HTTP {status}"
                )
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())


def test_a_calendar_write_401_refreshes_once_and_replays_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO)
            stub.route_token_sequence(
                REFRESH_TOKEN_PERSO,
                [
                    {"access_token": ACCESS_TOKEN_PERSO},
                    {"access_token": "access-token-perso-2"},
                ],
            )
            stub.route_sequence(
                "POST",
                CALENDAR_PRIMARY_PATH,
                [
                    {"status": 401, "body": {"error": {"code": 401}}},
                    {
                        "body": {
                            "id": "e11",
                            "htmlLink": "https://example.test/e11",
                        }
                    },
                ],
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_create_event",
                    {
                        "summary": "Réunion",
                        "start": "2026-11-12T09:00:00+01:00",
                        "end": "2026-11-12T10:00:00+01:00",
                    },
                )

                assert payload == {
                    "status": "ok",
                    "event_id": "e11",
                    "html_link": "https://example.test/e11",
                }
                created = stub.requests_for("POST", CALENDAR_PRIMARY_PATH)
                assert [request["authorization"] for request in created] == [
                    f"Bearer {ACCESS_TOKEN_PERSO}",
                    "Bearer access-token-perso-2",
                ]
                perso_refreshes = [
                    request
                    for request in stub.token_requests
                    if request["form"]["refresh_token"]
                    == [REFRESH_TOKEN_PERSO]
                ]
                # Démarrage + le seul rafraîchissement du 401.
                assert len(perso_refreshes) == 2

    asyncio.run(scenario())


def test_calendar_and_event_ids_are_percent_encoded_in_write_paths(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        calendar_id = "famille#holiday@group.calendar.google.com"
        event_id = "evt#1@google.com"
        decoded_path = (
            f"{CALENDAR_EVENTS_BASE}/{calendar_id}/events/{event_id}"
        )
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route("PATCH", decoded_path, body={"id": event_id})
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_update_event",
                    {
                        "event_id": event_id,
                        "calendar_id": calendar_id,
                        "summary": "Nouveau titre",
                    },
                )

                assert payload["status"] == "ok"
                assert payload["event_id"] == event_id
                [patched] = stub.calendar_requests
                assert patched["raw_path"] == (
                    f"{CALENDAR_EVENTS_BASE}/"
                    "famille%23holiday%40group.calendar.google.com/events/"
                    "evt%231%40google.com"
                )
                assert patched["path"] == decoded_path

    asyncio.run(scenario())


def test_calendar_write_traces_never_carry_id_summary_address_or_url(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "POST",
                CALENDAR_PRIMARY_PATH,
                body={
                    "id": "e-confidentiel",
                    "htmlLink": "https://example.test/e-confidentiel",
                },
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                payload = await call_tool(
                    session,
                    "calendar_create_event",
                    {
                        "summary": "Réunion confidentielle",
                        "start": "2026-11-12T09:00:00+01:00",
                        "end": "2026-11-12T10:00:00+01:00",
                        "description": "Sujet confidentiel",
                        "location": "Salle secrète",
                        "attendees": ["camille@example.test"],
                    },
                )
                assert payload["status"] == "ok"

                [called] = sink.of_kind("ToolCalled")
                assert called.payload["tool"] == "calendar_create_event"
                assert called.payload["account"] == "perso"
                assert called.payload["status"] == "ok"
                assert "result_count" not in called.payload
                assert set(called.payload) == {
                    "plugin",
                    "tool",
                    "account",
                    "duration_ms",
                    "status",
                }

                rendered = _traces_json(sink)
                for forbidden in (
                    *TOKEN_SECRETS,
                    ACCESS_TOKEN_PERSO,
                    "e-confidentiel",
                    "camille@example.test",
                    "Réunion confidentielle",
                    "Sujet confidentiel",
                    "Salle secrète",
                    CALENDAR_EVENTS_BASE,
                ):
                    assert forbidden not in rendered

    asyncio.run(scenario())


def test_a_create_without_title_or_dates_is_refused_before_any_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                for arguments in (
                    {"start": "2026-11-12T09:00:00+01:00",
                     "end": "2026-11-12T10:00:00+01:00"},
                    {"summary": "Réunion",
                     "end": "2026-11-12T10:00:00+01:00"},
                    {"summary": "Réunion",
                     "start": "2026-11-12T09:00:00+01:00"},
                ):
                    payload = await call_tool(
                        session, "calendar_create_event", arguments
                    )
                    assert payload["status"] == "error"
                    assert payload["message"].startswith(
                        "La création de l'événement d'agenda n'a pas abouti"
                    )

                assert stub.calendar_requests == []

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Dégradation multi-comptes, Console et smoke test (#141)
#
# Ces tests complètent le socle : plusieurs appels sur un compte cassé, la
# reprise à chaud, la retente globale quand tous les comptes tombent, la ligne
# de Console et l'absence de fuite dans les raisons tracées et le journal.
# ---------------------------------------------------------------------------


def test_a_refresh_reason_never_repeats_what_the_token_endpoint_returned(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
            )
            # Un intermédiaire hostile glisse un secret, une adresse et un
            # contenu dans le code d'erreur OAuth. Seul un code connu peut
            # devenir une raison tracée ; tout le reste retombe sur le statut.
            hostile = f"{REFRESH_TOKEN_PERSO} camille@example.test Objet secret"
            stub.route_token(
                REFRESH_TOKEN_PERSO,
                status=400,
                error=hostile,
                error_description=hostile,
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                assert app.plugins.states["google-workspace"] == "activated"

                [unavailable] = sink.of_kind("AccountUnavailable")
                assert unavailable.payload == {
                    "plugin": "google-workspace",
                    "account": "perso",
                    "reason": "HTTP 400",
                }
                [refresh_failed] = sink.of_kind("TokenRefreshFailed")
                assert refresh_failed.payload == {
                    "plugin": "google-workspace",
                    "account": "perso",
                    "reason": "HTTP 400",
                }

                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "gmail_search",
                        {"query": "x", "account": "perso"},
                    )
                assert payload["status"] == "error"

                rendered = _traces_json(sink)
                for forbidden in (
                    REFRESH_TOKEN_PERSO,
                    "camille@example.test",
                    "Objet secret",
                    hostile,
                ):
                    assert forbidden not in rendered
                    assert forbidden not in payload["message"]
                assert hostile not in json.dumps(app.snapshot(), default=str)

    asyncio.run(scenario())


def test_a_failed_account_is_retried_on_every_call_until_the_token_works(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
            )
            stub.route_token(
                REFRESH_TOKEN_PERSO, status=400, error="invalid_grant"
            )
            stub.route("GET", MESSAGES_PATH, body=gmail_message_list())
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                assert app.plugins.states["google-workspace"] == "activated"

                async with live_session(transport, live) as session:
                    first = await call_tool(
                        session,
                        "gmail_search",
                        {"query": "un", "account": "perso"},
                    )
                    second = await call_tool(
                        session,
                        "gmail_search",
                        {"query": "deux", "account": "perso"},
                    )
                    usable = await call_tool(
                        session,
                        "gmail_search",
                        {"query": "trois", "account": "pro"},
                    )

                    # Le refresh token redevient valide (révocation levée,
                    # propagation Google) : le prochain appel refait l'échange
                    # et réussit, sans redémarrage. Une réautorisation, elle,
                    # fournit un nouveau token et demande un redémarrage.
                    stub.route_token(
                        REFRESH_TOKEN_PERSO, access_token=ACCESS_TOKEN_PERSO
                    )
                    recovered = await call_tool(
                        session,
                        "gmail_search",
                        {"query": "quatre", "account": "perso"},
                    )

                expected = (
                    "Le compte « perso » est indisponible : "
                    "réautorise-le, puis réessaie."
                )
                assert first == {"status": "error", "message": expected}
                assert second == first
                assert usable["status"] == "ok"
                assert usable["account"] == "pro"
                assert recovered["status"] == "ok"
                assert recovered["account"] == "perso"

                # Démarrage + un échange à chaque appel sur le compte cassé,
                # puis un dernier échange après réautorisation.
                perso_exchanges = [
                    request
                    for request in stub.token_requests
                    if request["form"]["refresh_token"]
                    == [REFRESH_TOKEN_PERSO]
                ]
                assert len(perso_exchanges) == 4

                # Seuls le compte sain et le compte réparé lisent Gmail.
                assert [
                    request["authorization"]
                    for request in stub.requests_for("GET", MESSAGES_PATH)
                ] == [
                    f"Bearer {ACCESS_TOKEN_PRO}",
                    f"Bearer {ACCESS_TOKEN_PERSO}",
                ]

                # Le compte n'est tracé indisponible qu'au démarrage : les
                # appels suivants se lisent dans les Traces de Tool.
                assert len(sink.of_kind("AccountUnavailable")) == 1

                # Chaque échec émet ToolCalled puis ToolFailed ; un succès
                # n'émet que ToolCalled.
                called = sink.of_kind("ToolCalled")
                assert [
                    (entry.payload["account"], entry.payload["status"])
                    for entry in called
                ] == [
                    ("perso", "error"),
                    ("perso", "error"),
                    ("pro", "ok"),
                    ("perso", "ok"),
                ]
                failed = sink.of_kind("ToolFailed")
                assert [
                    (entry.payload["account"], entry.payload["status"])
                    for entry in failed
                ] == [("perso", "unavailable"), ("perso", "unavailable")]
                assert [
                    entry.kind
                    for entry in sink.entries
                    if entry.kind in ("ToolCalled", "ToolFailed")
                ] == [
                    "ToolCalled",
                    "ToolFailed",
                    "ToolCalled",
                    "ToolFailed",
                    "ToolCalled",
                    "ToolCalled",
                ]

    asyncio.run(scenario())


def test_the_console_snapshot_shows_the_activated_plugin_and_its_tools(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
            )
            stub.route_token(
                REFRESH_TOKEN_PERSO, status=400, error="invalid_grant"
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                # Un compte indisponible ne retire pas le plugin de la Console :
                # il reste activé et garde ses Tools.
                [google] = app.snapshot()["plugins"]
                assert google["name"] == "google-workspace"
                assert google["state"] == "activated"
                assert google["tools"] == [
                    "calendar_create_event",
                    "calendar_delete_event",
                    "calendar_list_events",
                    "calendar_update_event",
                    "contacts_search",
                    "gmail_read",
                    "gmail_reply",
                    "gmail_search",
                    "gmail_send",
                ]
                assert google["declaration"]["api_version"] == 1
                assert app.plugins.tools.owner_of("gmail_search") == (
                    "google-workspace"
                )
                async with live_session(transport, live) as _session:
                    assert set(declared(live)) == {
                        "calendar_create_event",
                        "calendar_delete_event",
                        "calendar_list_events",
                        "calendar_update_event",
                        "contacts_search",
                        "gmail_read",
                        "gmail_reply",
                        "gmail_search",
                        "gmail_send",
                    }

                [activated] = [
                    entry
                    for entry in sink.of_kind("PluginState")
                    if entry.payload["state"] == "activated"
                ]
                assert activated.payload["tools"] == 9
                assert states(sink) == ["activated"]

    asyncio.run(scenario())


def test_all_accounts_failing_degrades_the_start_and_retries_globally(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            for refresh_token in (REFRESH_TOKEN_PRO, REFRESH_TOKEN_PERSO):
                stub.route_token(
                    refresh_token, status=400, error="invalid_grant"
                )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(
                registry=registry, loader=loader, retry_interval=0.05
            ) as (app, live, transport, _):
                # Aucun compte : le serveur démarre quand même, sans Tools.
                # La retente de fond peut déjà avoir émis une seconde Trace :
                # seule la première et l'état courant comptent ici.
                assert states(sink)[0] == "failed"
                assert app.plugins.states["google-workspace"] == "failed"
                assert app.plugins.tools.specs() == ()
                [google] = app.snapshot()["plugins"]
                assert google["name"] == "google-workspace"
                assert google["state"] == "failed"
                assert google["tools"] == []
                assert google["retry"] is True
                assert google["reason"].startswith("PluginUnavailable")
                first = sink.of_kind("PluginState")[0]
                assert first.payload["state"] == "failed"
                assert first.payload["reason"].startswith("PluginUnavailable")

                # La retente globale tourne toute seule et reste en échec
                # tant que Google refuse les deux comptes.
                await wait_until(
                    lambda: any(
                        entry.payload.get("retry") is True
                        and entry.payload["state"] == "failed"
                        for entry in sink.of_kind("PluginState")
                    ),
                    timeout=2.0,
                )
                assert app.plugins.states["google-workspace"] == "failed"

                # Refresh tokens réparés : la retente suivante active le plugin.
                program_tokens(stub)
                stub.route("GET", MESSAGES_PATH, body=gmail_message_list())
                await wait_for_state(app, "google-workspace", "activated")

                [google] = app.snapshot()["plugins"]
                assert google["name"] == "google-workspace"
                assert google["state"] == "activated"
                assert google["tools"] == [
                    "calendar_create_event",
                    "calendar_delete_event",
                    "calendar_list_events",
                    "calendar_update_event",
                    "contacts_search",
                    "gmail_read",
                    "gmail_reply",
                    "gmail_search",
                    "gmail_send",
                ]
                assert google["declaration"]["api_version"] == 1
                [last] = [
                    entry
                    for entry in sink.of_kind("PluginState")
                    if entry.payload["state"] == "activated"
                ]
                assert last.payload["tools"] == 9
                # La reprise réussie n'annonce plus de retente : seule une
                # trace ``failed`` encore retentée porte ``retry``.
                assert "retry" not in last.payload

                unavailable = sink.of_kind("AccountUnavailable")
                assert {entry.payload["account"] for entry in unavailable} == {
                    "pro",
                    "perso",
                }
                assert all(
                    entry.payload["reason"] == "invalid_grant"
                    for entry in unavailable
                )
                rendered = _traces_json(sink)
                assert all(
                    secret not in rendered for secret in TOKEN_SECRETS
                )

                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "gmail_search", {"query": "réparé"}
                    )
                    assert payload["status"] == "ok"

    asyncio.run(scenario())


def test_no_secret_reaches_the_log_the_snapshot_or_an_error_message(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "logs" / "server.log"
    log = FileTraceSink(log_path, min_level="debug")
    try:

        async def scenario() -> None:
            async with GoogleStub() as stub:
                stub.route_token(
                    REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
                )
                stub.route_token(
                    REFRESH_TOKEN_PERSO, status=400, error="invalid_grant"
                )
                stub.route("GET", MESSAGES_PATH, body=gmail_message_list())
                plugin_sink = RecordingTraceSink()
                registry, loader = build_plugins(
                    tmp_path, plugin_sink, stub.base_url, extra_sinks=[log]
                )
                async with running_app(
                    registry=registry, loader=loader, extra_sinks=[log]
                ) as (app, live, transport, app_sink):
                    async with live_session(transport, live) as session:
                        ok = await call_tool(
                            session,
                            "gmail_search",
                            {"query": "requête confidentielle", "account": "pro"},
                        )
                        failed = await call_tool(
                            session,
                            "gmail_search",
                            {"query": "requête confidentielle", "account": "perso"},
                        )

                    assert ok["status"] == "ok"
                    assert failed["status"] == "error"
                    # Les secrets partent réellement : le test n'est pas vide.
                    assert {
                        request["form"]["refresh_token"][0]
                        for request in stub.token_requests
                    } == {REFRESH_TOKEN_PRO, REFRESH_TOKEN_PERSO}

                    traces = json.dumps(
                        [
                            entry.payload
                            for entry in (
                                *app_sink.entries,
                                *plugin_sink.entries,
                            )
                        ],
                        default=str,
                    )
                    snapshot = json.dumps(app.snapshot(), default=str)
                    plugin_states = json.dumps(
                        app.plugins.states, default=str
                    )
                    for forbidden in (
                        *TOKEN_SECRETS,
                        ACCESS_TOKEN_PRO,
                        "requête confidentielle",
                    ):
                        assert forbidden not in traces
                        assert forbidden not in snapshot
                        assert forbidden not in plugin_states
                        assert forbidden not in failed["message"]
                        assert forbidden not in str(ok.get("message", ""))

        asyncio.run(scenario())
    finally:
        log.close()

    rendered = log_path.read_text(encoding="utf-8")
    # Le journal porte bien les Traces de dégradation et d'appel : l'absence
    # de secret est une vraie absence, pas un fichier vide.
    assert "AccountUnavailable" in rendered
    assert "TokenRefreshFailed" in rendered
    assert "ToolCalled" in rendered
    assert "ToolFailed" in rendered
    for secret in TOKEN_SECRETS:
        assert secret not in rendered
    assert "requête confidentielle" not in rendered


# ---------------------------------------------------------------------------
# Envoi et réponse Gmail (#138)
# ---------------------------------------------------------------------------


def test_gmail_send_composes_a_utf8_plain_text_mime_and_returns_the_new_ids(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "POST",
                SEND_PATH,
                body={"id": "envoye-1", "threadId": "fil-envoye-1"},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_send",
                    {
                        "to": ["Camille <camille@example.test>"],
                        "subject": "Déjeuner à Noël",
                        "body": "Bonjour,\n\nOn se voit à midi ?\n\nÀ bientôt — café.",
                    },
                )

                assert payload == {
                    "status": "ok",
                    "message_id": "envoye-1",
                    "thread_id": "fil-envoye-1",
                }

                [request] = stub.requests_for("POST", SEND_PATH)
                assert request["authorization"] == f"Bearer {ACCESS_TOKEN_PERSO}"
                assert request["content_type"].startswith("application/json")
                assert set(request["body"]) == {"raw"}

                raw = base64.urlsafe_b64decode(request["body"]["raw"])
                # En-têtes encodés (RFC 2047) et lignes RFC 5322 en CRLF.
                assert b"Subject: =?utf-8?" in raw
                assert b"\r\n" in raw

                message = gmail_sent_message(request)
                assert message["To"] == "Camille <camille@example.test>"
                assert message["Subject"] == "Déjeuner à Noël"
                assert message.get_content_type() == "text/plain"
                assert message.get_content_charset() == "utf-8"
                assert message.get_content() == (
                    "Bonjour,\n\nOn se voit à midi ?\n\nÀ bientôt — café.\n"
                )

    asyncio.run(scenario())


def test_gmail_send_puts_cc_and_bcc_in_the_same_mime(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "POST",
                SEND_PATH,
                body={"id": "envoye-2", "threadId": "fil-envoye-2"},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_send",
                    {
                        "to": ["camille@example.test"],
                        "cc": ["bureau@example.test", "equipe@example.test"],
                        "bcc": ["archive@example.test"],
                        "subject": "Compte rendu",
                        "body": "Voici le compte rendu.",
                    },
                )

                assert payload["status"] == "ok"
                [request] = stub.requests_for("POST", SEND_PATH)
                message = gmail_sent_message(request)
                assert message["To"] == "camille@example.test"
                assert message["Cc"] == (
                    "bureau@example.test, equipe@example.test"
                )
                assert message["Bcc"] == "archive@example.test"
                assert message.get_content() == "Voici le compte rendu.\n"

    asyncio.run(scenario())


def test_gmail_send_refuses_an_empty_recipient_or_body_before_any_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                missing_to = await call_tool(
                    session,
                    "gmail_send",
                    {"subject": "Objet", "body": "Corps"},
                )
                empty_to = await call_tool(
                    session,
                    "gmail_send",
                    {"to": [], "subject": "Objet", "body": "Corps"},
                )
                empty_body = await call_tool(
                    session,
                    "gmail_send",
                    {
                        "to": ["camille@example.test"],
                        "subject": "Objet",
                        "body": "   ",
                    },
                )
                unknown = await call_tool(
                    session,
                    "gmail_send",
                    {
                        "to": ["camille@example.test"],
                        "subject": "Objet",
                        "body": "Corps",
                        "account": "inconnu",
                    },
                )

                for payload in (missing_to, empty_to):
                    assert payload["status"] == "error"
                    assert payload["message"].startswith(
                        "L'envoi du mail n'a pas abouti"
                    )
                    assert "destinataire" in payload["message"]
                assert empty_body["status"] == "error"
                assert "corps" in empty_body["message"]
                assert unknown["status"] == "error"
                assert "inconnu" in unknown["message"]

                assert stub.requests_for("POST", SEND_PATH) == []

    asyncio.run(scenario())


def test_gmail_send_turns_a_refused_send_into_a_french_sentence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "POST",
                SEND_PATH,
                status=403,
                body={"error": {"code": 403}},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_send",
                    {
                        "to": ["camille@example.test"],
                        "subject": "Objet",
                        "body": "Corps",
                    },
                )

                assert payload["status"] == "error"
                assert payload["message"].startswith(
                    "L'envoi du mail n'a pas abouti"
                )
                assert "refusé" in payload["message"]
                assert len(stub.requests_for("POST", SEND_PATH)) == 1
                assert len(stub.token_requests) == 2
                assert sink.of_kind("ToolFailed")[-1].payload["status"] == (
                    "HTTP 403"
                )

    asyncio.run(scenario())


def test_a_401_on_send_refreshes_once_and_replays_the_same_raw_message(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            stub.route_token(
                REFRESH_TOKEN_PRO, access_token=ACCESS_TOKEN_PRO
            )
            stub.route_token_sequence(
                REFRESH_TOKEN_PERSO,
                [
                    {"access_token": ACCESS_TOKEN_PERSO},
                    {"access_token": "access-token-perso-2"},
                ],
            )
            stub.route_sequence(
                "POST",
                SEND_PATH,
                [
                    {"status": 401, "body": {"error": {"code": 401}}},
                    {"body": {"id": "envoye-3", "threadId": "fil-envoye-3"}},
                ],
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_send",
                    {
                        "to": ["camille@example.test"],
                        "subject": "Après 401",
                        "body": "Corps",
                    },
                )

                assert payload == {
                    "status": "ok",
                    "message_id": "envoye-3",
                    "thread_id": "fil-envoye-3",
                }
                sent = stub.requests_for("POST", SEND_PATH)
                assert [request["authorization"] for request in sent] == [
                    f"Bearer {ACCESS_TOKEN_PERSO}",
                    "Bearer access-token-perso-2",
                ]
                # Le rejeu porte exactement le même message.
                assert sent[0]["body"]["raw"] == sent[1]["body"]["raw"]

    asyncio.run(scenario())


def test_gmail_reply_reuses_the_thread_recipient_and_reply_headers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            original = gmail_reply_source(
                "m1",
                thread_id="t1",
                references="<racine@example.test>",
            )
            stub.route("GET", f"{MESSAGES_PATH}/m1", body=original)
            stub.route(
                "POST",
                SEND_PATH,
                body={"id": "reponse-1", "threadId": "t1"},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_reply",
                    {"message_id": "m1", "body": "Oui, à midi."},
                )

                assert payload == {
                    "status": "ok",
                    "message_id": "reponse-1",
                    "thread_id": "t1",
                }

                [fetched] = stub.requests_for("GET", f"{MESSAGES_PATH}/m1")
                assert fetched["query"]["format"] == ["metadata"]
                assert fetched["query"]["metadataHeaders"] == [
                    "From",
                    "To",
                    "Cc",
                    "Subject",
                    "Message-ID",
                    "References",
                ]

                [posted] = stub.requests_for("POST", SEND_PATH)
                assert posted["body"]["threadId"] == "t1"
                message = gmail_sent_message(posted)
                assert message["To"] == "Camille <camille@example.test>"
                assert message["Cc"] is None
                assert message["Subject"] == "Re: Déjeuner jeudi"
                assert message["In-Reply-To"] == "<original@example.test>"
                assert message["References"] == (
                    "<racine@example.test> <original@example.test>"
                )
                assert message.get_content() == "Oui, à midi.\n"
                assert stub.requests_for("GET", "/gmail/v1/users/me/profile") == []

    asyncio.run(scenario())


def test_gmail_reply_decodes_an_encoded_subject_and_sender(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                f"{MESSAGES_PATH}/m1",
                body=gmail_reply_source(
                    "m1",
                    sender=ENCODED_SENDER,
                    subject=ENCODED_SUBJECT,
                ),
            )
            stub.route(
                "POST",
                SEND_PATH,
                body={"id": "reponse-encoded", "threadId": "t1"},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_reply",
                    {"message_id": "m1", "body": "Oui."},
                )

                assert payload["status"] == "ok"
                [posted] = stub.requests_for("POST", SEND_PATH)
                message = gmail_sent_message(posted)
                assert message["Subject"] == "Re: Déjeuner jeudi"
                assert message["To"] == (
                    "Camille é <camille@example.test>"
                )

    asyncio.run(scenario())


def test_gmail_reply_all_adds_the_original_recipients_without_self(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            original = gmail_reply_source(
                "m1",
                thread_id="t1",
                to="perso@example.test, equipe@example.test",
                cc="bureau@example.test, camille@example.test",
            )
            stub.route("GET", f"{MESSAGES_PATH}/m1", body=original)
            stub.route(
                "GET",
                "/gmail/v1/users/me/profile",
                body={"emailAddress": "perso@example.test"},
            )
            stub.route(
                "POST",
                SEND_PATH,
                body={"id": "reponse-2", "threadId": "t1"},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "gmail_reply",
                    {
                        "message_id": "m1",
                        "body": "Je confirme.",
                        "reply_all": True,
                    },
                )

                assert payload["status"] == "ok"
                [posted] = stub.requests_for("POST", SEND_PATH)
                message = gmail_sent_message(posted)
                assert message["To"] == (
                    "Camille <camille@example.test>, equipe@example.test"
                )
                assert message["Cc"] == "bureau@example.test"
                assert "perso@example.test" not in (
                    (message["To"] or "") + (message["Cc"] or "")
                )
                assert len(stub.requests_for("GET", "/gmail/v1/users/me/profile")) == 1

    asyncio.run(scenario())


def test_gmail_reply_does_not_stack_the_re_prefix(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                f"{MESSAGES_PATH}/m1",
                body=gmail_reply_source("m1", subject="RE: Déjeuner jeudi"),
            )
            stub.route(
                "POST",
                SEND_PATH,
                body={"id": "reponse-3", "threadId": "t1"},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                await call_tool(
                    session,
                    "gmail_reply",
                    {"message_id": "m1", "body": "D'accord."},
                )

                [posted] = stub.requests_for("POST", SEND_PATH)
                assert gmail_sent_message(posted)["Subject"] == (
                    "RE: Déjeuner jeudi"
                )

    asyncio.run(scenario())


def test_gmail_reply_refuses_an_empty_body_or_a_missing_original(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                no_id = await call_tool(
                    session, "gmail_reply", {"body": "Bonjour."}
                )
                empty_body = await call_tool(
                    session,
                    "gmail_reply",
                    {"message_id": "m1", "body": "   "},
                )
                unknown = await call_tool(
                    session,
                    "gmail_reply",
                    {
                        "message_id": "m1",
                        "body": "Bonjour.",
                        "account": "inconnu",
                    },
                )
                # Aucune route n'est programmée : le message est introuvable.
                missing = await call_tool(
                    session,
                    "gmail_reply",
                    {"message_id": "absent", "body": "Bonjour."},
                )

                for payload in (no_id, empty_body, missing):
                    assert payload["status"] == "error"
                    assert payload["message"].startswith(
                        "La réponse au mail n'a pas abouti"
                    )
                assert "identifiant" in no_id["message"]
                assert "corps" in empty_body["message"]
                assert "inconnu" in unknown["message"]
                assert "introuvable" in missing["message"]
                assert stub.requests_for("GET", f"{MESSAGES_PATH}/m1") == []
                assert stub.requests_for("POST", SEND_PATH) == []

    asyncio.run(scenario())


def test_gmail_send_and_reply_declarations_are_exact(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                live,
                _,
                _,
                _,
            ):
                tools = declared(live)
                assert tools["gmail_send"].description == (
                    "Envoie un mail en texte brut."
                )
                assert dict(tools["gmail_send"].parameters_json_schema) == {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": (
                                "Destinataires principaux, au moins un ; des "
                                "adresses e-mail, jamais des noms."
                            ),
                        },
                        "cc": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Destinataires en copie ; des adresses e-mail."
                            ),
                        },
                        "bcc": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Destinataires en copie cachée ; des adresses "
                                "e-mail."
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
                                "Clé du compte Google à utiliser ; le compte "
                                "par défaut quand il est absent."
                            ),
                        },
                    },
                    "required": ["to", "subject", "body"],
                }
                assert tools["gmail_reply"].description == (
                    "Répond dans le fil d'un message Gmail."
                )
                assert dict(tools["gmail_reply"].parameters_json_schema) == {
                    "type": "object",
                    "properties": {
                        "message_id": {
                            "type": "string",
                            "description": (
                                "Identifiant du message auquel répondre."
                            ),
                        },
                        "body": {
                            "type": "string",
                            "description": (
                                "Corps de la réponse, en texte brut."
                            ),
                        },
                        "reply_all": {
                            "type": "boolean",
                            "description": (
                                "Répondre aussi aux autres destinataires du "
                                "message d'origine."
                            ),
                        },
                        "account": {
                            "type": "string",
                            "description": (
                                "Clé du compte Google à utiliser ; le compte "
                                "par défaut quand il est absent."
                            ),
                        },
                    },
                    "required": ["message_id", "body"],
                }
                # La règle de relecture et de confirmation explicite est
                # portée par l'instruction composée depuis #136.
                assert "attends un « oui » explicite avant d'envoyer" in (
                    live.instruction()
                )

    asyncio.run(scenario())


def test_send_and_reply_traces_never_carry_addresses_subjects_or_bodies(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                f"{MESSAGES_PATH}/m1",
                body=gmail_reply_source(
                    "m1",
                    sender="camille@example.test",
                    subject="Objet très confidentiel",
                ),
            )
            stub.route(
                "POST",
                SEND_PATH,
                body={"id": "envoye-4", "threadId": "fil-4"},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                sent = await call_tool(
                    session,
                    "gmail_send",
                    {
                        "to": ["camille@example.test"],
                        "subject": "Objet très confidentiel",
                        "body": "Corps très confidentiel",
                    },
                )
                replied = await call_tool(
                    session,
                    "gmail_reply",
                    {
                        "message_id": "m1",
                        "body": "Réponse très confidentielle",
                    },
                )

                assert sent["status"] == "ok"
                assert replied["status"] == "ok"

                called = sink.of_kind("ToolCalled")
                assert [entry.payload["tool"] for entry in called] == [
                    "gmail_send",
                    "gmail_reply",
                ]
                assert all(
                    "result_count" not in entry.payload for entry in called
                )

                rendered = _traces_json(sink)
                for forbidden in (
                    *TOKEN_SECRETS,
                    ACCESS_TOKEN_PERSO,
                    "camille@example.test",
                    "Objet très confidentiel",
                    "Corps très confidentiel",
                    "Réponse très confidentielle",
                    "raw",
                    MESSAGES_PATH,
                    "/gmail/",
                ):
                    assert forbidden not in rendered

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Contrat de ``contacts_search`` : carnet, autres contacts, choix de l'adresse
# ---------------------------------------------------------------------------


def test_contacts_search_is_declared_with_its_schema(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                live,
                _,
                _,
                _,
            ):
                tools = declared(live)
                assert tools["contacts_search"].description == (
                    CONTACTS_SEARCH_DESCRIPTION
                )
                assert dict(
                    tools["contacts_search"].parameters_json_schema
                ) == CONTACTS_SEARCH_PARAMETERS

    asyncio.run(scenario())


def test_contacts_search_merges_the_carnet_and_other_contacts_by_address(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c1",
                        name="Aubane Cros",
                        emails=(
                            ("aubane.crs@gmail.com", "home"),
                            ("aubane@corp.example", "work"),
                        ),
                    ),
                    # Une fiche sans adresse n'aide pas à envoyer un mail.
                    people_person("people/c2", name="Bruno"),
                ),
            )
            stub.route(
                "GET",
                PEOPLE_OTHER_PATH,
                body=other_contacts(
                    people_person(
                        "otherContacts/c1",
                        name="Aubane",
                        emails=(("AUBANE.CRS@gmail.com", "other"),),
                    )
                ),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )

                assert payload == {
                    "status": "ok",
                    "account": "perso",
                    "results": [
                        {
                            "name": "Aubane Cros",
                            "emails": [
                                {
                                    "address": "aubane.crs@gmail.com",
                                    "type": "home",
                                },
                                {
                                    "address": "aubane@corp.example",
                                    "type": "work",
                                },
                            ],
                            "source": "carnet",
                        }
                    ],
                    "truncated": False,
                }

                [connections] = stub.requests_for(
                    "GET", PEOPLE_CONNECTIONS_PATH
                )
                assert connections["authorization"] == f"Bearer {ACCESS_TOKEN_PERSO}"
                assert connections["query"] == {
                    "personFields": ["names,emailAddresses"],
                    "pageSize": ["500"],
                }
                [others] = stub.requests_for("GET", PEOPLE_OTHER_PATH)
                assert others["authorization"] == f"Bearer {ACCESS_TOKEN_PERSO}"
                assert others["query"] == {
                    "readMask": ["names,emailAddresses"],
                    "pageSize": ["500"],
                }

                [called] = sink.of_kind("ToolCalled")
                assert called.payload["result_count"] == 1

    asyncio.run(scenario())


def test_contacts_search_matches_every_token_in_the_name_or_an_address(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c1",
                        name="Aubane Cros",
                        emails=(("aubane.crs@gmail.com", "home"),),
                    ),
                    people_person(
                        "people/c2",
                        name="Aubane Martin",
                        emails=(("aubane.martin@example.test", "work"),),
                    ),
                ),
            )
            stub.route(
                "GET",
                PEOPLE_OTHER_PATH,
                body=other_contacts(
                    people_person(
                        "otherContacts/c1",
                        name=None,
                        emails=(("diane@example.test", "home"),),
                    )
                ),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                both = await call_tool(
                    session, "contacts_search", {"query": "AUBANE"}
                )
                assert [person["name"] for person in both["results"]] == [
                    "Aubane Cros",
                    "Aubane Martin",
                ]

                accented = await call_tool(
                    session,
                    "contacts_search",
                    {"query": "aubane crs"},
                )
                assert [person["name"] for person in accented["results"]] == [
                    "Aubane Cros"
                ]

                by_address = await call_tool(
                    session,
                    "contacts_search",
                    {"query": "DIANE@example.test"},
                )
                assert by_address["results"] == [
                    {
                        "name": "",
                        "emails": [
                            {
                                "address": "diane@example.test",
                                "type": "home",
                            }
                        ],
                        "source": "autres",
                    }
                ]

                missing = await call_tool(
                    session, "contacts_search", {"query": "zzz"}
                )
                assert missing["results"] == []
                assert missing["truncated"] is False

    asyncio.run(scenario())


def test_contacts_search_forgives_one_misheard_letter_in_a_name(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c1",
                        name="Aubane Cros",
                        emails=(("aubane.crs@gmail.com", "home"),),
                    )
                ),
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                # « Aubagne » est ce que la reconnaissance vocale a compris.
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubagne"}
                )
                assert payload == {
                    "status": "ok",
                    "account": "perso",
                    "results": [
                        {
                            "name": "Aubane Cros",
                            "emails": [
                                {
                                    "address": "aubane.crs@gmail.com",
                                    "type": "home",
                                }
                            ],
                            "source": "carnet",
                        }
                    ],
                    "truncated": False,
                    "approximate": True,
                }

    asyncio.run(scenario())


def test_contacts_search_reads_the_local_part_of_an_unnamed_contact(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route("GET", PEOPLE_CONNECTIONS_PATH, body=people_connections())
            stub.route(
                "GET",
                PEOPLE_OTHER_PATH,
                body=other_contacts(
                    # Une entrée ``otherContacts`` sans nom n'existe que par
                    # son adresse : la partie locale est le seul indice.
                    people_person(
                        "otherContacts/c1",
                        name=None,
                        emails=(("aubane.crs@gmail.com", "other"),),
                    )
                ),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubagne"}
                )
                assert payload == {
                    "status": "ok",
                    "account": "perso",
                    "results": [
                        {
                            "name": "",
                            "emails": [
                                {
                                    "address": "aubane.crs@gmail.com",
                                    "type": "other",
                                }
                            ],
                            "source": "autres",
                        }
                    ],
                    "truncated": False,
                    "approximate": True,
                }

    asyncio.run(scenario())


def test_contacts_search_keeps_an_exact_result_free_of_approximate_neighbours(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c1",
                        name="Aubane Cros",
                        emails=(("aubane.crs@gmail.com", "home"),),
                    ),
                    # La passe tolérante l'aurait rapproché de « aubane » ; il
                    # ne doit jamais s'inviter dans un retour exact.
                    people_person(
                        "people/c2",
                        name="Aubagne Martin",
                        emails=(("aubagne.martin@example.test", "work"),),
                    ),
                ),
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )
                assert [person["name"] for person in payload["results"]] == [
                    "Aubane Cros"
                ]
                assert "approximate" not in payload

    asyncio.run(scenario())


def test_contacts_search_bounds_its_approximations(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c1",
                        name="Anna",
                        emails=(("anna@example.test", "home"),),
                    ),
                    people_person(
                        "people/c2",
                        name="Bernard",
                        emails=(("bernard@example.test", "home"),),
                    ),
                    people_person(
                        "people/c3",
                        name="Martine",
                        emails=(("martine@example.test", "home"),),
                    ),
                    people_person(
                        "people/c4",
                        name="Bruno",
                        emails=(("bruno@gmail.com", "home"),),
                    ),
                    people_person(
                        "people/c5",
                        name="Alexandre",
                        emails=(("alexandre@example.test", "home"),),
                    ),
                    people_person(
                        "people/c6",
                        name="Bertrand",
                        emails=(("bertrand@example.test", "home"),),
                    ),
                ),
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                for query in (
                    # Trois lettres : trop court pour ne pas désigner tout le
                    # monde à une faute près.
                    "ana",
                    # Une faute, mais l'initiale change.
                    "ennard",
                    # Deux fautes sur un mot de sept lettres.
                    "martena",
                    # Le domaine d'une adresse n'est jamais comparé.
                    "gmiel",
                ):
                    payload = await call_tool(
                        session, "contacts_search", {"query": query}
                    )
                    assert payload["results"] == []
                    assert "approximate" not in payload

                # Deux fautes passent à partir de huit lettres.
                payload = await call_tool(
                    session, "contacts_search", {"query": "alexanora"}
                )
                assert [person["name"] for person in payload["results"]] == [
                    "Alexandre"
                ]
                assert payload["approximate"] is True

                # Huit lettres : la seconde faute est déjà tolérée.
                payload = await call_tool(
                    session, "contacts_search", {"query": "bertrent"}
                )
                assert [person["name"] for person in payload["results"]] == [
                    "Bertrand"
                ]
                assert payload["approximate"] is True

    asyncio.run(scenario())


def test_contacts_search_tolerates_a_typo_on_every_token(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c1",
                        name="Aubane Cros",
                        emails=(("aubane.crs@gmail.com", "home"),),
                    ),
                    people_person(
                        "people/c2",
                        name="Aubane Martin",
                        emails=(("aubane.martin@example.test", "work"),),
                    ),
                ),
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                # Les deux mots sont mal entendus : pas d'ancrage exact.
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubagne croz"}
                )
                assert [person["name"] for person in payload["results"]] == [
                    "Aubane Cros"
                ]
                assert payload["approximate"] is True

    asyncio.run(scenario())


def test_contacts_search_orders_approximate_results_by_distance_then_name(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c1",
                        name="Aubane Cros",
                        emails=(("aubane.crs@gmail.com", "home"),),
                    ),
                    people_person(
                        "people/c2",
                        name="Aubagne Cros",
                        emails=(("aubagne.cros@example.test", "work"),),
                    ),
                ),
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubagne croz"}
                )
                # « Aubagne Cros » n'a qu'une faute (« croz »), « Aubane Cros »
                # en a deux : la meilleure correspondance passe devant.
                assert [person["name"] for person in payload["results"]] == [
                    "Aubagne Cros",
                    "Aubane Cros",
                ]

    asyncio.run(scenario())


def test_contacts_search_caps_approximate_results_at_five(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    *(
                        people_person(
                            f"people/c{index}",
                            name=f"Aubane {surname}",
                            emails=((f"aubane.{index}@example.test", "home"),),
                        )
                        for index, surname in enumerate(
                            (
                                "Cros",
                                "Dupont",
                                "Fournier",
                                "Girard",
                                "Martin",
                                "Petit",
                            )
                        )
                    )
                ),
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubagne"}
                )
                # Cinq au plus : au-delà, le modèle devrait lire trop de noms
                # à voix haute pour une simple confirmation.
                assert [person["name"] for person in payload["results"]] == [
                    "Aubane Cros",
                    "Aubane Dupont",
                    "Aubane Fournier",
                    "Aubane Girard",
                    "Aubane Martin",
                ]
                assert payload["truncated"] is True

                narrower = await call_tool(
                    session,
                    "contacts_search",
                    {"query": "aubagne", "max_results": 2},
                )
                assert [person["name"] for person in narrower["results"]] == [
                    "Aubane Cros",
                    "Aubane Dupont",
                ]
                assert narrower["truncated"] is True

    asyncio.run(scenario())


def test_contacts_search_sorts_and_caps_results_with_a_truncated_flag(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c3",
                        name="Aubane Petit",
                        emails=(("aubane.petit@example.test", "home"),),
                    ),
                    people_person(
                        "people/c1",
                        name="Aubane Cros",
                        emails=(("aubane.crs@gmail.com", "home"),),
                    ),
                    people_person(
                        "people/c2",
                        name="Aubane Martin",
                        emails=(("aubane.martin@example.test", "work"),),
                    ),
                ),
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "contacts_search",
                    {"query": "aubane", "max_results": 2},
                )
                assert payload["results"] == [
                    {
                        "name": "Aubane Cros",
                        "emails": [
                            {
                                "address": "aubane.crs@gmail.com",
                                "type": "home",
                            }
                        ],
                        "source": "carnet",
                    },
                    {
                        "name": "Aubane Martin",
                        "emails": [
                            {
                                "address": "aubane.martin@example.test",
                                "type": "work",
                            }
                        ],
                        "source": "carnet",
                    },
                ]
                assert payload["truncated"] is True

                [request] = stub.requests_for("GET", PEOPLE_CONNECTIONS_PATH)
                # Une recherche vocale ne pagine pas : pas de jeton de page.
                assert "pageToken" not in request["query"]

    asyncio.run(scenario())


def test_contacts_search_follows_pages_until_the_match(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route_sequence(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                [
                    {
                        "body": people_connections(
                            people_person(
                                "people/c1",
                                name="Camille",
                                emails=(("camille@example.test", "home"),),
                            ),
                            next_page_token="page-2",
                        )
                    },
                    {
                        "body": people_connections(
                            people_person(
                                "people/c2",
                                name="Aubane Cros",
                                emails=(("aubane.crs@gmail.com", "home"),),
                            )
                        )
                    },
                ],
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )
                assert [person["name"] for person in payload["results"]] == [
                    "Aubane Cros"
                ]

                first, second = stub.requests_for(
                    "GET", PEOPLE_CONNECTIONS_PATH
                )
                assert "pageToken" not in first["query"]
                assert second["query"]["pageToken"] == ["page-2"]

    asyncio.run(scenario())


def test_contacts_search_stops_after_the_page_cap_and_flags_the_cut(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route_sequence(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                [
                    {
                        "body": people_connections(
                            next_page_token=f"page-{index}"
                        )
                    }
                    for index in range(2, 7)
                ],
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )
                assert payload["results"] == []
                assert payload["truncated"] is True
                assert (
                    len(stub.requests_for("GET", PEOPLE_CONNECTIONS_PATH)) == 4
                )

    asyncio.run(scenario())


def test_an_empty_contacts_query_is_refused_before_any_people_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "   "}
                )
                assert payload == {
                    "status": "error",
                    "message": (
                        "La recherche dans les contacts n'a pas abouti : "
                        "la requête est vide."
                    ),
                }
                assert (
                    stub.requests_for("GET", PEOPLE_CONNECTIONS_PATH) == []
                )

    asyncio.run(scenario())


def _google_scope_error() -> dict[str, Any]:
    return {
        "error": {
            "code": 403,
            "message": "Request had insufficient authentication scopes.",
            "status": "PERMISSION_DENIED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                }
            ],
        }
    }


def test_contacts_search_names_the_api_and_the_scope_to_grant(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                status=403,
                body=_google_scope_error(),
            )
            stub.route(
                "GET",
                PEOPLE_OTHER_PATH,
                status=403,
                body=_google_scope_error(),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )
                assert payload == {
                    "status": "error",
                    "message": (
                        "La recherche dans les contacts n'a pas abouti : "
                        "le carnet du compte « perso » n'est pas accessible : "
                        "active l'API People et réautorise le compte, "
                        "puis réessaie."
                    ),
                }
                [failed] = sink.of_kind("ToolFailed")
                assert failed.payload["status"] == "insufficient_scope"
                assert "aubane" not in _traces_json(sink)

    asyncio.run(scenario())


def test_a_plain_contacts_403_keeps_the_generic_french_sentence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                status=403,
                body={"error": {"code": 403}},
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )
                assert payload == {
                    "status": "error",
                    "message": (
                        "La recherche dans les contacts n'a pas abouti : "
                        "l'accès au carnet de ce compte est refusé."
                    ),
                }

    asyncio.run(scenario())


def test_a_failing_contacts_call_is_a_french_sentence_without_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                status=500,
                body={"error": {"code": 500}},
            )
            stub.route(
                "GET",
                PEOPLE_OTHER_PATH,
                status=500,
                body={"error": {"code": 500}},
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )
                assert payload == {
                    "status": "error",
                    "message": (
                        "La recherche dans les contacts n'a pas abouti : "
                        "le service Contacts est indisponible (réponse 500)."
                    ),
                }
                assert (
                    len(stub.requests_for("GET", PEOPLE_CONNECTIONS_PATH)) == 1
                )

    asyncio.run(scenario())


def test_a_401_on_contacts_refreshes_once_and_replays_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route_token_sequence(
                REFRESH_TOKEN_PERSO,
                [
                    {"access_token": ACCESS_TOKEN_PERSO},
                    {"access_token": "access-token-renouvele"},
                ],
            )
            stub.route_sequence(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                [
                    {"status": 401, "body": {"error": {"code": 401}}},
                    {
                        "body": people_connections(
                            people_person(
                                "people/c1",
                                name="Aubane Cros",
                                emails=(("aubane.crs@gmail.com", "home"),),
                            )
                        )
                    },
                ],
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )
                assert payload["status"] == "ok"

                first, second = stub.requests_for(
                    "GET", PEOPLE_CONNECTIONS_PATH
                )
                assert first["authorization"] == f"Bearer {ACCESS_TOKEN_PERSO}"
                assert second["authorization"] == (
                    "Bearer access-token-renouvele"
                )

    asyncio.run(scenario())


def test_the_contacts_probe_traces_a_missing_scope_without_degrading(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET", PEOPLE_ME_PATH, status=403, body=_google_scope_error()
            )
            async with google_workspace_session(tmp_path, stub) as (
                app,
                _,
                _,
                sink,
                _,
            ):
                assert app.plugins.states["google-workspace"] == "activated"
                probes = sorted(
                    (entry.payload for entry in sink.of_kind("ContactsScopeMissing")),
                    key=lambda payload: str(payload["account"]),
                )
                assert probes == [
                    {
                        "plugin": "google-workspace",
                        "account": "perso",
                        "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                    },
                    {
                        "plugin": "google-workspace",
                        "account": "pro",
                        "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                    },
                ]
                [activated] = [
                    entry
                    for entry in sink.of_kind("PluginState")
                    if entry.payload["state"] == "activated"
                ]
                assert activated.payload["tools"] == 9

    asyncio.run(scenario())


def test_contacts_traces_never_carry_a_name_address_query_or_url(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c1",
                        name="Aubane Cros",
                        emails=(("aubane.crs@gmail.com", "home"),),
                    )
                ),
            )
            stub.route("GET", PEOPLE_OTHER_PATH, body=other_contacts())
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )
                assert payload["status"] == "ok"

                # La passe tolérante ne trace pas davantage que l'exacte.
                approximate = await call_tool(
                    session, "contacts_search", {"query": "aubagne"}
                )
                assert approximate["status"] == "ok"
                assert approximate["approximate"] is True

                called = sink.of_kind("ToolCalled")
                assert len(called) == 2
                for entry in called:
                    assert entry.payload["tool"] == "contacts_search"
                    assert entry.payload["account"] == "perso"
                    assert entry.payload["status"] == "ok"
                    assert entry.payload["result_count"] == 1
                    assert set(entry.payload) == {
                        "plugin",
                        "tool",
                        "account",
                        "duration_ms",
                        "status",
                        "result_count",
                    }

                rendered = _traces_json(sink)
                for forbidden in (
                    *TOKEN_SECRETS,
                    ACCESS_TOKEN_PERSO,
                    "Aubane",
                    "aubane",
                    "aubagne",
                    "aubane.crs@gmail.com",
                    PEOPLE_CONNECTIONS_PATH,
                    PEOPLE_OTHER_PATH,
                ):
                    assert forbidden not in rendered

    asyncio.run(scenario())


def test_gmail_send_refuses_a_name_and_points_to_contacts_search(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                sink,
                _,
            ):
                named = await call_tool(
                    session,
                    "gmail_send",
                    {
                        "to": ["Aubane"],
                        "subject": "Objet",
                        "body": "Corps",
                    },
                )
                copied = await call_tool(
                    session,
                    "gmail_send",
                    {
                        "to": ["aubane.crs@gmail.com"],
                        "cc": ["Aubane"],
                        "subject": "Objet",
                        "body": "Corps",
                    },
                )

                assert named["status"] == "error"
                assert "n'est pas une adresse e-mail" in named["message"]
                assert "contacts_search" in named["message"]
                # Une phrase d'erreur ne répète jamais la valeur fautive.
                assert "Aubane" not in named["message"]
                assert copied["status"] == "error"
                assert copied["message"].startswith(
                    "L'envoi du mail n'a pas abouti"
                )
                assert "copie" in copied["message"]
                assert "Aubane" not in copied["message"]

                statuses = [
                    entry.payload["status"]
                    for entry in sink.of_kind("ToolFailed")
                ]
                assert statuses == ["invalid_to", "invalid_recipient"]
                assert stub.requests_for("POST", SEND_PATH) == []

    asyncio.run(scenario())


def test_two_addresses_without_a_shared_address_stay_two_people(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with GoogleStub() as stub:
            program_tokens(stub)
            stub.route(
                "GET",
                PEOPLE_CONNECTIONS_PATH,
                body=people_connections(
                    people_person(
                        "people/c1",
                        name="Aubane Cros",
                        emails=(("aubane.crs@gmail.com", "home"),),
                    )
                ),
            )
            stub.route(
                "GET",
                PEOPLE_OTHER_PATH,
                body=other_contacts(
                    people_person(
                        "otherContacts/c1",
                        name="Aubane Cros",
                        emails=(("aubane.autre@example.test", "other"),),
                    )
                ),
            )
            async with google_workspace_session(tmp_path, stub) as (
                _,
                _,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "contacts_search", {"query": "aubane"}
                )
                assert payload["results"] == [
                    {
                        "name": "Aubane Cros",
                        "emails": [
                            {
                                "address": "aubane.autre@example.test",
                                "type": "other",
                            }
                        ],
                        "source": "autres",
                    },
                    {
                        "name": "Aubane Cros",
                        "emails": [
                            {
                                "address": "aubane.crs@gmail.com",
                                "type": "home",
                            }
                        ],
                        "source": "carnet",
                    },
                ]
                assert payload["truncated"] is False

    asyncio.run(scenario())
