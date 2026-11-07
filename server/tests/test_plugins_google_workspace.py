"""Tests du plugin ``google-workspace`` : socle multi-comptes et recherche Gmail.

Chaque test traverse la couture réelle : App → faux Gemini Live → registre →
plugin copié → faux serveur Google (token OAuth, Gmail). Rien n'est simulé en
dehors du modèle et de Google : les Tools, l'instruction, les Traces et les
phrases d'erreur s'observent à la frontière. Aucun helper privé du plugin n'est
touché.

Le fichier est organisé par section : manifeste et secrets, validation de la
configuration, démarrage et échange OAuth, sélection du compte, contrat de
``gmail_search``, cache et rafraîchissement du jeton, pannes HTTP, puis
vérification transverse (instruction, Console, Traces sans fuite).

Les tickets #137 à #141 étendent ce fichier ; les sections existantes laissent
la place aux Tools Gmail puis Calendar sans réorganiser le socle.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import RecordingTraceSink
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
    declared,
    gmail_message_list,
    gmail_metadata,
    gmail_read_message,
    gmail_read_thread,
    google_workspace_session,
    install_google_workspace,
    live_session,
    program_tokens,
    running_app,
    states,
    wait_for_state,
)
from server.adapters.plugins import PluginLoader
from server.adapters.plugins.loader import PluginLoadError
from server.app.secrets import EnvSecretResolver
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

INSTRUCTION = INSTRUCTION_TEMPLATE.replace("{accounts}", "pro, perso").replace(
    "{default}", "perso"
)

MESSAGES_PATH = "/gmail/v1/users/me/messages"
THREADS_PATH = "/gmail/v1/users/me/threads"
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
        "version": "0.1.0",
        "description": "Mails Gmail et agenda Google : lecture, envoi, événements",
        "platform": "server",
        "entrypoint": "plugin.py",
        "config": [
            {"key": "accounts", "type": "string", "required": True},
            {"key": "default_account", "type": "string"},
            {"key": "api_base_url", "type": "url"},
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
                assert row == {
                    "name": "google-workspace",
                    "state": "activated",
                    "tools": ["gmail_read", "gmail_search"],
                }

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
                    assert declared(live).keys() == {
                        "gmail_read",
                        "gmail_search",
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
                assert set(tools) == {"gmail_search", "gmail_read"}
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
                assert row["tools"] == ["gmail_read", "gmail_search"]

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
