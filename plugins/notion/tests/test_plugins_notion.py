"""Tests du plugin ``notion`` : pages Notion en voix.

Chaque test traverse la couture réelle : App → faux Gemini Live → registre →
plugin copié → faux serveur Notion. Rien n'est simulé en dehors du modèle et de
la REST : les Tools, l'instruction, l'Annonce, les Traces et le journal rendu
s'observent à la frontière. Aucun helper privé du plugin n'est touché.

Le fichier est organisé par section : manifeste/probe/recherche, lecture,
filtre par mot, création, ajout et renommage, remplacement, corbeille,
tableaux/colonnes, puis vérification transverse.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, AsyncIterator

import pytest
import websockets

from testkit import FakeJudge, RecordingTraceSink, register, url, wait_until
from notion_harness import (
    DEFAULT_PARENT,
    PLUGIN_DIR,
    SECRET,
    SECRET_VARIABLE,
    build_plugins,
    call_tool,
    declared,
    install_notion,
    live_session,
    markdown_body as _markdown_body,
    notion_session as _notion_session,
    page_object as _page,
    program_probe,
    running_app,
    states,
)
from notion_stub import NotionStub
from server.adapters.plugins import PluginLoader
from server.adapters.plugins.loader import PluginLoadError
from server.app.secrets import EnvSecretResolver
from server.app.trace import FileTraceSink
from server.core.plugins import PluginRegistry

# ---------------------------------------------------------------------------
# Constantes partagées
# ---------------------------------------------------------------------------

PAGE_ID = "e" * 32
TARGET = "f" * 32
PAGE = "c" * 32
MD_PAGE = "9a" * 16

INSTRUCTION = (
    "Les Tools Notion ne voient que les pages partagées avec la connexion, "
    "et rien d'autre ; les bases de données et les data sources sont "
    "hors périmètre, et seul le titre s'écrit parmi les propriétés. "
    "N'invente jamais un identifiant ni une URL de page : n'utilise que ceux "
    "rendus par `search_pages` ou donnés par l'utilisateur.\n"
    "`search_pages` : quand l'utilisateur nomme une page sans identifiant ni "
    "URL. Si plusieurs titres se ressemblent, lis les titres candidats à voix "
    "haute et fais choisir ; suis `next_cursor` s'il reste des résultats.\n"
    "`read_page` : quand l'utilisateur veut le contenu, un détail, ou « la "
    "suite ». Reprends avec `offset` et `next_offset`. Utilise `query` pour "
    "retrouver un mot dans les paragraphes de cette page seulement, avec son "
    "titre de section ; ne cherche jamais dans tout le workspace.\n"
    "`create_page` : pour créer une page sous la page parente par défaut, ou "
    "sous un parent que l'utilisateur nomme ; confirme le parent quand il en "
    "nomme un.\n"
    "`update_page` : ajoute du contenu par défaut ; ne remplace le corps "
    "qu'après une confirmation explicite, avec `mode=replace` et "
    "`confirm=true`. Pour corriger un titre dicté, renomme.\n"
    "`trash_page` : seulement après confirmation explicite, après avoir lu le "
    "titre et annoncé les `child_pages` emportées.\n"
    "`restore_page` : pour annuler une mise à la corbeille.\n"
    "Markdown garanti en écriture : titres 1 à 4, paragraphes, listes à puces "
    "et numérotées, cases à cocher, citations, blocs de code avec un nom de "
    "langage court, gras, italique, barré et code en ligne, liens, tableaux "
    "GFM en pipe tables, et "
    '<columns><column ratio="50">…</column><column ratio="50">…</column>'
    "</columns> pour les colonnes. Limites : 2 000 caractères par texte "
    "enrichi, 100 blocs par ajout, 5 000 blocs à la création ; au-delà, "
    "Notion refuse plutôt que de couper en silence."
)
SEARCH_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Titre recherché, en mots.",
        },
        "cursor": {
            "type": "string",
            "description": "Curseur rendu par l'appel précédent.",
        },
    },
    "required": ["query"],
}
READ_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "page": {
            "type": "string",
            "description": "Identifiant ou URL de la page.",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Bloc (ou correspondance) de départ. Défaut 0.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Blocs (ou correspondances) par appel. Défaut 20.",
        },
        "query": {
            "type": "string",
            "description": "Mot à chercher dans les paragraphes de cette page.",
        },
    },
    "required": ["page"],
}
CREATE_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "Titre de la page.",
        },
        "content": {
            "type": "string",
            "description": "Corps en Markdown Notion (v2). Optionnel.",
        },
        "parent": {
            "type": "string",
            "description": (
                "Identifiant ou URL d'une page parente. "
                "Défaut : la page configurée."
            ),
        },
    },
    "required": ["title"],
}
UPDATE_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "page": {
            "type": "string",
            "description": "Identifiant ou URL de la page.",
        },
        "title": {
            "type": "string",
            "description": "Nouveau titre. Optionnel.",
        },
        "content": {
            "type": "string",
            "description": "Markdown à ajouter ou à substituer. Optionnel.",
        },
        "mode": {
            "type": "string",
            "enum": ["append", "replace"],
            "description": "Défaut : append.",
        },
        "confirm": {
            "type": "boolean",
            "description": "Obligatoire à true pour mode=replace.",
        },
    },
    "required": ["page"],
}
TRASH_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "page": {
            "type": "string",
            "description": "Identifiant ou URL de la page.",
        },
        "confirm": {
            "type": "boolean",
            "description": "Doit valoir true ; sinon le Tool refuse.",
        },
    },
    "required": ["page"],
}
RESTORE_PARAMETERS: dict = {
    "type": "object",
    "properties": {
        "page": {
            "type": "string",
            "description": "Identifiant ou URL de la page en corbeille.",
        },
    },
    "required": ["page"],
}
DESCRIPTIONS: dict[str, str] = {
    "search_pages": (
        "Cherche des pages Notion par titre et rend les candidats avec leur "
        "URL."
    ),
    "read_page": (
        "Lit une page Notion en Markdown, par blocs, avec ses propriétés ; "
        "filtre éventuellement les paragraphes contenant un mot."
    ),
    "create_page": (
        "Crée une page Notion sous une page parente, avec un titre et un "
        "corps Markdown."
    ),
    "update_page": (
        "Ajoute du contenu à une page, remplace son corps sur confirmation, "
        "ou la renomme."
    ),
    "trash_page": (
        "Met une page Notion à la corbeille, de façon réversible."
    ),
    "restore_page": "Sort une page Notion de la corbeille.",
}
PARAMETERS: dict[str, dict] = {
    "search_pages": SEARCH_PARAMETERS,
    "read_page": READ_PARAMETERS,
    "create_page": CREATE_PARAMETERS,
    "update_page": UPDATE_PARAMETERS,
    "trash_page": TRASH_PARAMETERS,
    "restore_page": RESTORE_PARAMETERS,
}
TOOL_NAMES = [
    "create_page",
    "read_page",
    "restore_page",
    "search_pages",
    "trash_page",
    "update_page",
]



# ---------------------------------------------------------------------------
# Fixtures et helpers partagés
# ---------------------------------------------------------------------------


def _title(page: dict) -> str:
    for prop in page["properties"].values():
        if prop.get("type") == "title":
            return "".join(
                part.get("plain_text", "") for part in prop.get("title", [])
            )
    return "(sans titre)"


def _summary(page: dict) -> dict:
    """La forme ``page`` rendue au modèle : id, titre, URL."""
    return {"id": page["id"], "title": _title(page), "url": page["url"]}


def _writes(stub: NotionStub) -> list[dict]:
    """Toute requête qui n'est pas un GET : un refus ne doit rien écrire."""
    return [request for request in stub.requests if request["method"] != "GET"]


def _append_body(content: str) -> dict:
    return {
        "type": "insert_content",
        "insert_content": {
            "content": content,
            "position": {"type": "end"},
        },
        "markdown_version": "v2",
    }


def _replace_body(content: str) -> dict:
    """Corps exact du remplacement : jamais de suppression de contenu."""
    return {
        "type": "replace_content",
        "replace_content": {
            "new_str": content,
            "allow_deleting_content": False,
        },
        "markdown_version": "v2",
    }


def _rename_body(key: str, title: str) -> dict:
    return {
        "properties": {
            key: {
                "title": [
                    {"type": "text", "text": {"content": title}}
                ]
            }
        }
    }


def _children(
    *types: str, has_more: bool = False, cursor: str | None = None
) -> dict:
    """Liste de blocs enfants, un bloc par type demandé."""
    return {
        "object": "list",
        "results": [
            {"object": "block", "id": f"{index:032x}", "type": kind}
            for index, kind in enumerate(types)
        ],
        "has_more": has_more,
        "next_cursor": cursor,
    }


def _children_path(page_id: str = PAGE_ID) -> str:
    return f"/v1/blocks/{page_id}/children"


def _page_path(page_id: str = PAGE_ID) -> str:
    return f"/v1/pages/{page_id}"


async def _served_page(
    stub: NotionStub, page: str, markdown: str, title: str = "Atelier"
) -> None:
    """Programme la page et son Markdown sur le faux serveur."""
    stub.route("GET", f"/v1/pages/{page}", body=_page(page, title))
    stub.route(
        "GET", f"/v1/pages/{page}/markdown", body=_markdown_body(markdown)
    )




# ---------------------------------------------------------------------------
# Manifeste, démarrage, recherche
# ---------------------------------------------------------------------------


def test_manifest_declares_only_static_identity_and_requirements() -> None:
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))

    assert manifest == {
        "api_version": 1,
        "name": "notion",
        "version": "0.1.0",
        "description": (
            "Pages Notion : recherche, lecture Markdown, création, "
            "modification, corbeille"
        ),
        "platform": "server",
        "entrypoint": "plugin.py",
        "config": [
            {"key": "parent_page_id", "type": "string", "required": True},
            {"key": "base_url", "type": "url"},
        ],
        "secrets": [{"key": "token", "required": True}],
    }

def test_absent_token_fails_the_start_and_names_the_variable(
    tmp_path: Path,
) -> None:
    plugins_dir, state_path = install_notion(tmp_path, "http://127.0.0.1:1")
    loader = PluginLoader(
        directory=plugins_dir,
        state_path=state_path,
        secrets=EnvSecretResolver({}),
        trace_sink=RecordingTraceSink(),
    )

    with pytest.raises(PluginLoadError) as excinfo:
        asyncio.run(
            loader.load(PluginRegistry(), push_event=lambda source, text: None)
        )

    assert SECRET_VARIABLE in str(excinfo.value)

@pytest.mark.parametrize("status", [401, 403])
def test_refused_probe_degrades_the_start_then_repairs_itself(
    tmp_path: Path, status: int
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            stub.route(
                "GET",
                "/v1/users/me",
                status=status,
                body={"object": "error", "status": status},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(
                registry=registry, loader=loader, retry_interval=0.1
            ) as (app, live, transport, _):
                # Le jeton est refusé : Athena démarre quand même, dégradée.
                assert states(sink)[0] == "failed"
                assert set(states(sink)) == {"failed"}
                assert app.plugins.states["notion"] == "failed"
                assert app.plugins.tools.specs() == ()
                assert str(status) in str(
                    sink.of_kind("PluginState")[0].payload["reason"]
                )
                [failed] = sink.of_kind("ProbeFailed")
                assert failed.payload["status"] == status
                # Avant la réparation, seule la sonde du compte a été appelée.
                before = list(stub.requests)
                assert all(
                    request["path"] == "/v1/users/me" for request in before
                )

                # Le serveur se répare : la retente active le plugin.
                stub.route("GET", "/v1/users/me", body={"object": "user"})
                stub.route(
                    "GET",
                    f"/v1/pages/{DEFAULT_PARENT}",
                    body={"object": "page", "id": DEFAULT_PARENT},
                )
                await wait_for_state(app, "notion", "activated")

                # Les Tools arrivent à la Connexion Live suivante.
                async with live_session(transport, live):
                    assert "search_pages" in declared(live)

    asyncio.run(scenario())

def test_unreachable_notion_degrades_the_start_without_killing_the_app(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        sink = RecordingTraceSink()
        registry, loader = build_plugins(tmp_path, sink, "http://127.0.0.1:1")
        async with running_app(registry=registry, loader=loader) as (
            app,
            _live,
            _transport,
            app_sink,
        ):
            assert states(sink) == ["failed"]
            assert app.plugins.states["notion"] == "failed"
            assert app.plugins.tools.specs() == ()
            # Le serveur démarre quand même : l'Intégration ne bloque pas la voix.
            assert app_sink.of_kind("started")
            [failed] = sink.of_kind("ProbeFailed")
            assert failed.payload["reason"] != "timeout"

    asyncio.run(scenario())

def test_a_probe_5xx_is_traced_as_degraded_without_blocking_the_start(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub, users_status=503)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                # Notion répond : c'est sa sonde qui est en défaut, pas le plugin.
                assert app.plugins.states["notion"] == "activated"
                [degraded] = sink.of_kind("ProbeDegraded")
                assert degraded.payload["status"] == 503
                assert not sink.of_kind("ProbeFailed")
                snapshot = app.snapshot()["plugins"]
                assert len(snapshot) == 1
                [notion] = snapshot
                assert notion["name"] == "notion"
                assert notion["state"] == "activated"

                async with live_session(transport, live):
                    assert "search_pages" in declared(live)

    asyncio.run(scenario())


def test_a_5xx_on_the_parent_probe_degrades_instead_of_failing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub, parent_status=503)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                # Un 5xx sur la page parente est une sonde en défaut, pas une
                # configuration fausse : le plugin s'active.
                assert app.plugins.states["notion"] == "activated"
                [degraded] = sink.of_kind("ProbeDegraded")
                assert degraded.payload["status"] == 503
                assert not sink.of_kind("ProbeFailed")
                assert app_sink.of_kind("started")

                async with live_session(transport, live):
                    assert "search_pages" in declared(live)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [403, 404])
def test_an_unshared_parent_page_fails_the_start_naming_the_configuration(
    tmp_path: Path, status: int
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            stub.route("GET", "/v1/users/me", body={"object": "user"})
            stub.route(
                "GET",
                f"/v1/pages/{DEFAULT_PARENT}",
                status=status,
                body={"object": "error", "status": status},
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )

            with pytest.raises(PluginLoadError) as excinfo:
                await loader.load(
                    registry, push_event=lambda source, text: None
                )

            message = str(excinfo.value)
            assert "parent_page_id" in message
            assert DEFAULT_PARENT in message
            assert "partag" in message
            assert registry.states == {}

    asyncio.run(scenario())

def test_an_absent_parent_page_id_fails_the_start_naming_the_configuration(
    tmp_path: Path,
) -> None:
    plugins_dir, state_path = install_notion(tmp_path, "http://127.0.0.1:1")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    del state["config"]["notion"]["parent_page_id"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    loader = PluginLoader(
        directory=plugins_dir,
        state_path=state_path,
        secrets=EnvSecretResolver({SECRET_VARIABLE: SECRET}),
        trace_sink=RecordingTraceSink(),
    )

    with pytest.raises(PluginLoadError) as excinfo:
        asyncio.run(
            loader.load(PluginRegistry(), push_event=lambda source, text: None)
        )

    assert "parent_page_id" in str(excinfo.value)

def test_an_invalid_parent_page_id_fails_without_any_http_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            stub.route("GET", "/v1/users/me", body={"object": "user"})
            registry, loader = build_plugins(
                tmp_path,
                RecordingTraceSink(),
                stub.base_url,
                parent="pas-un-identifiant",
            )

            with pytest.raises(PluginLoadError) as excinfo:
                await loader.load(
                    registry, push_event=lambda source, text: None
                )

            assert "parent_page_id" in str(excinfo.value)
            # Aucun appel HTTP : la configuration est refusée avant le réseau.
            assert stub.requests == []

    asyncio.run(scenario())

def test_a_notion_url_parent_is_reduced_to_its_last_identifier(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            stub.route("GET", "/v1/users/me", body={"object": "user"})
            stub.route(
                "GET",
                f"/v1/pages/{DEFAULT_PARENT}",
                body={"object": "page", "id": DEFAULT_PARENT},
            )
            registry, loader = build_plugins(
                tmp_path,
                RecordingTraceSink(),
                stub.base_url,
                parent=(
                    f"https://www.notion.so/atelier/Page-{DEFAULT_PARENT}?pvs=4"
                ),
            )

            await loader.load(registry, push_event=lambda source, text: None)

            assert registry.states["notion"] == "activated"
            assert stub.requests_for(
                "GET", f"/v1/pages/{DEFAULT_PARENT}"
            )

    asyncio.run(scenario())

async def wait_for_state(app, plugin: str, state: str, timeout: float = 2.0) -> None:
    await wait_until(
        lambda: app.plugins.states.get(plugin) == state, timeout=timeout
    )

def test_search_pages_is_declared_and_returns_page_candidates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            first = "a" * 32
            second = "b" * 32
            stub.route(
                "POST",
                "/v1/search",
                body={
                    "object": "list",
                    "results": [
                        _page(first, "Projet Atlas"),
                        _page(second, "Courses"),
                    ],
                    "has_more": True,
                    "next_cursor": "curseur-2",
                },
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                assert app.plugins.states["notion"] == "activated"
                # Le Tool est visible dans l'instantané de la Console.
                snapshot = app.snapshot()["plugins"]
                assert len(snapshot) == 1
                [notion] = snapshot
                assert notion["name"] == "notion"
                assert notion["state"] == "activated"

                async with live_session(transport, live) as session:
                    tools = declared(live)
                    assert dict(
                        tools["search_pages"].parameters_json_schema
                    ) == SEARCH_PARAMETERS
                    assert "pages Notion" in tools["search_pages"].description

                    payload = await call_tool(
                        session, "search_pages", {"query": "projet"}
                    )

                assert payload == {
                    "status": "ok",
                    "count": 2,
                    "results": [
                        {
                            "id": first,
                            "title": "Projet Atlas",
                            "url": f"https://www.notion.so/{first}",
                            "last_edited_time": "2026-10-01T10:00:00.000Z",
                        },
                        {
                            "id": second,
                            "title": "Courses",
                            "url": f"https://www.notion.so/{second}",
                            "last_edited_time": "2026-10-01T10:00:00.000Z",
                        },
                    ],
                    "has_more": True,
                    "next_cursor": "curseur-2",
                }

                # La requête sortante est exacte : filtre page, tri par dernière
                # modification décroissante, taille 10, aucun curseur au premier
                # appel.
                [request] = stub.requests_for("POST", "/v1/search")
                assert request["authorization"] == f"Bearer {SECRET}"
                assert request["notion_version"] == "2026-03-11"
                assert request["body"] == {
                    "query": "projet",
                    "filter": {"property": "object", "value": "page"},
                    "sort": {
                        "direction": "descending",
                        "timestamp": "last_edited_time",
                    },
                    "page_size": 10,
                }

                # Une Trace Search : requête, nombre de résultats, pagination,
                # durée, statut. Jamais le jeton.
                [search] = sink.of_kind("Search")
                assert search.payload["query"] == "projet"
                assert search.payload["results"] == 2
                assert search.payload["has_more"] is True
                assert search.payload["status"] == "ok"
                assert isinstance(search.payload["duration_ms"], (int, float))
                assert SECRET not in json.dumps(sink.entries[-1].payload)
                assert all(
                    SECRET not in json.dumps(entry.payload)
                    for entry in sink.entries
                )
                assert not sink.of_kind("SessionClosed")

    asyncio.run(scenario())

def test_search_pages_transmits_a_cursor_only_when_it_is_non_empty(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page_id = "c" * 32
            stub.route(
                "POST",
                "/v1/search",
                body={
                    "object": "list",
                    "results": [_page(page_id, "Unique")],
                    "has_more": False,
                    "next_cursor": None,
                },
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    empty = await call_tool(
                        session,
                        "search_pages",
                        {"query": "premier", "cursor": ""},
                    )
                    followed = await call_tool(
                        session,
                        "search_pages",
                        {"query": "second", "cursor": "curseur-2"},
                    )

                assert empty["next_cursor"] is None
                assert empty["has_more"] is False
                assert followed["count"] == 1
                first, second = stub.requests_for("POST", "/v1/search")
                assert "start_cursor" not in first["body"]
                assert second["body"]["start_cursor"] == "curseur-2"

    asyncio.run(scenario())

def test_search_results_are_capped_at_ten_and_tolerate_missing_fields(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            results = [
                {"object": "page", "id": "a" * 32, "properties": {}},
                {"object": "page", "id": "b" * 32},
                *[_page(f"{index:032x}", f"Page {index}") for index in range(2, 12)],
            ]
            stub.route(
                "POST",
                "/v1/search",
                body={
                    "object": "list",
                    "results": results,
                    "has_more": False,
                    "next_cursor": None,
                },
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "search_pages", {"query": "beaucoup"}
                    )

                # Dix résultats au plus, dans l'ordre du serveur.
                assert payload["count"] == 10
                assert payload["results"][0]["title"] == "(sans titre)"
                assert payload["results"][1]["title"] == "(sans titre)"
                assert [
                    result["title"] for result in payload["results"][2:]
                ] == [f"Page {index}" for index in range(2, 10)]

    asyncio.run(scenario())

def test_search_status_failures_render_french_sentences_and_are_never_retried(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with live_session(transport, live) as session:
                    cases = [
                        (401, "ATHENA_PLUGIN_NOTION_TOKEN"),
                        (403, "capacité"),
                        (404, "introuvable ou non partagée avec la connexion"),
                        (429, "débit"),
                        (529, "débit"),
                        (500, "500"),
                    ]
                    for status, fragment in cases:
                        stub.route(
                            "POST",
                            "/v1/search",
                            status=status,
                            body={"object": "error", "status": status},
                        )
                        before = len(
                            stub.requests_for("POST", "/v1/search")
                        )
                        payload = await call_tool(
                            session,
                            "search_pages",
                            {"query": f"statut {status}"},
                        )

                        assert payload["status"] == "error"
                        assert fragment in payload["message"]
                        # Exactement une requête : aucun retry automatique.
                        assert (
                            len(stub.requests_for("POST", "/v1/search"))
                            == before + 1
                        )
                        trace = sink.of_kind("Search")[-1].payload
                        assert trace["status"] == "error"
                        assert trace["reason"] == f"HTTP {status}"
                        assert trace["results"] == 0

                # Une panne d'appel ne change pas l'état du plugin ni la Session.
                assert app.plugins.states["notion"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())

def test_search_timeout_returns_a_french_error_without_retrying(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "POST",
                "/v1/search",
                delay=1.0,
                body={"object": "list", "results": []},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(
                tmp_path, sink, stub.base_url, timeout=0.2
            )
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "search_pages", {"query": "trop lent"}
                    )

                assert payload["status"] == "error"
                assert "temps" in payload["message"]
                assert len(stub.requests_for("POST", "/v1/search")) == 1
                trace = sink.of_kind("Search")[-1].payload
                assert trace["status"] == "error"
                assert trace["reason"] == "timeout"
                assert app.plugins.states["notion"] == "activated"

    asyncio.run(scenario())

def test_an_unreachable_notion_during_a_session_is_a_sentence_not_a_crash(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stub = NotionStub()
        await stub.start()
        try:
            program_probe(stub)
            stub.route(
                "POST",
                "/v1/search",
                body={"object": "list", "results": []},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with live_session(transport, live) as session:
                    before = await call_tool(
                        session, "search_pages", {"query": "avant la panne"}
                    )
                    assert before["status"] == "ok"
                    await stub.stop()

                    unreachable = await call_tool(
                        session, "search_pages", {"query": "pendant la panne"}
                    )

                assert unreachable["status"] == "error"
                assert "injoignable" in unreachable["message"]
                assert app.plugins.states["notion"] == "activated"
                assert not app_sink.of_kind("SessionClosed")
        finally:
            await stub.stop()

    asyncio.run(scenario())

def test_an_unreadable_search_body_is_an_error_not_zero_results(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    stub.route(
                        "POST", "/v1/search", raw_body=b"{pas du json"
                    )
                    broken = await call_tool(
                        session, "search_pages", {"query": "json cassé"}
                    )
                    stub.route(
                        "POST",
                        "/v1/search",
                        raw_body=b'{"object": "list"}',
                    )
                    shapeless = await call_tool(
                        session, "search_pages", {"query": "sans résultats"}
                    )

                assert broken["status"] == "error"
                assert "illisible" in broken["message"]
                assert shapeless["status"] == "error"
                assert "illisible" in shapeless["message"]
                assert [
                    entry.payload["reason"] for entry in sink.of_kind("Search")
                ] == ["unreadable body", "unreadable body"]

    asyncio.run(scenario())

def test_the_token_never_reaches_a_trace_a_log_or_the_snapshot(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "logs" / "server.log"
    log = FileTraceSink(log_path, min_level="debug")
    try:

        async def scenario() -> None:
            async with NotionStub() as stub:
                program_probe(stub)
                stub.route(
                    "POST",
                    "/v1/search",
                    body={
                        "object": "list",
                        "results": [_page("d" * 32, "Secrète")],
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
                plugin_sink = RecordingTraceSink()
                registry, loader = build_plugins(
                    tmp_path,
                    plugin_sink,
                    stub.base_url,
                    extra_sinks=[log],
                )
                async with running_app(
                    registry=registry, loader=loader, extra_sinks=[log]
                ) as (app, live, transport, app_sink):
                    async with live_session(transport, live) as session:
                        payload = await call_tool(
                            session, "search_pages", {"query": "page secrète"}
                        )

                    assert payload["status"] == "ok"
                    # Le jeton part réellement au serveur : le test n'est pas vide.
                    assert set(
                        request["authorization"] for request in stub.requests
                    ) == {f"Bearer {SECRET}"}

                    traces = [
                        entry.payload
                        for entry in (*app_sink.entries, *plugin_sink.entries)
                    ]
                    assert SECRET not in json.dumps(traces, default=str)
                    assert SECRET not in json.dumps(
                        app.snapshot(), default=str
                    )
                    assert SECRET not in json.dumps(
                        app.plugins.states, default=str
                    )

        asyncio.run(scenario())
    finally:
        log.close()

    rendered = log_path.read_text(encoding="utf-8")
    # Le journal porte bien la sonde et la recherche : l'absence de jeton est
    # une vraie absence, pas un fichier vide.
    assert "PluginState" in rendered
    assert "Search" in rendered
    assert SECRET not in rendered

def test_an_announcement_session_declares_no_notion_tool(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(
                registry=registry,
                loader=loader,
                announce_prompt="ANNONCE",
                judge=FakeJudge(message="La porte est ouverte"),
            ) as (app, live, transport, app_sink):
                assert app.plugins.states["notion"] == "activated"
                # Le Tool existe bien dans le registre : c'est le gate de
                # l'Annonce, pas une absence de plugin, qui le retire.
                assert app.tools.owner_of("search_pages") == "notion"

                async with websockets.connect(url(transport)) as ws:
                    await register(
                        ws, transport, client_name="desktop", drain=False
                    )
                    await wait_until(
                        lambda: any(
                            entry.kind == "ClientRegistered"
                            for entry in app_sink.entries
                        )
                    )
                    app.push_event("matrix", "porte ouverte")
                    # La fenêtre de regroupement dure 5 s (ADR-0007).
                    await wait_until(
                        lambda: bool(app_sink.of_kind("EventJudge")),
                        timeout=7.0,
                    )

                # La Connexion Live d'Annonce est ouverte sans aucun outil.
                assert live.declarations() == []
                assert app.plugins.states["notion"] == "activated"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Lecture Markdown
# ---------------------------------------------------------------------------


def test_read_page_is_declared_with_its_pagination_bounds(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            async with _notion_session(tmp_path, stub) as (app, live, _, _, _):
                [plugin] = app.snapshot()["plugins"]
                assert plugin["name"] == "notion"
                assert plugin["state"] == "activated"
                tools = declared(live)
                assert "read_page" in tools
                assert (
                    dict(tools["read_page"].parameters_json_schema)
                    == READ_PARAMETERS
                )
                assert "Markdown" in tools["read_page"].description

    asyncio.run(scenario())

def test_read_page_fetches_the_page_and_the_explicit_v2_markdown(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "e" * 32
            markdown = "# Titre\n\nUn paragraphe."
            stub.route(
                "GET", f"/v1/pages/{page}", body=_page(page, "Atelier")
            )
            stub.route(
                "GET",
                f"/v1/pages/{page}/markdown",
                body=_markdown_body(markdown),
            )
            async with _notion_session(tmp_path, stub) as (
                app,
                _live,
                session,
                _,
                app_sink,
            ):
                payload = await call_tool(
                    session, "read_page", {"page": page}
                )

                assert payload == {
                    "status": "ok",
                    "page": {
                        "id": page,
                        "title": "Atelier",
                        "url": f"https://www.notion.so/{page}",
                        "last_edited_time": "2026-10-01T10:00:00.000Z",
                        "properties": {"Titre": "Atelier"},
                    },
                    "content": markdown,
                    "offset": 0,
                    "next_offset": 2,
                    "has_more": False,
                    "total_blocks": 2,
                    "truncated": False,
                    "unknown_block_ids": [],
                    "query": None,
                    "match_count": 0,
                }

                # Deux requêtes exactes, mêmes en-têtes, version Markdown
                # explicite.
                [page_request] = stub.requests_for(
                    "GET", f"/v1/pages/{page}"
                )
                [markdown_request] = stub.requests_for(
                    "GET", f"/v1/pages/{page}/markdown"
                )
                assert page_request["authorization"] == f"Bearer {SECRET}"
                assert page_request["notion_version"] == "2026-03-11"
                assert markdown_request["authorization"] == f"Bearer {SECRET}"
                assert markdown_request["notion_version"] == "2026-03-11"
                assert markdown_request["query"] == {
                    "markdown_version": ["v2"]
                }

                # La Session survit et le plugin reste activé.
                assert app.plugins.states["notion"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())

PAGINATED_MARKDOWN = (
    "# Titre\n"
    "\n"
    "Premier paragraphe\n"
    "sur deux lignes.\n"
    "\n"
    "| A | B |\n"
    "| --- | --- |\n"
    "| 1 | 2 |\n"
    "\n"
    "<columns>\n"
    '<column ratio="50">\n'
    "Gauche\n"
    "</column>\n"
    '<column ratio="50">\n'
    "Droite\n"
    "</column>\n"
    "</columns>\n"
    "\n"
    "- un\n"
    "- deux\n"
    "\n"
    "## Suite\n"
    "\n"
    "Dernier paragraphe.\n"
)

TABLE_BLOCK = "| A | B |\n| --- | --- |\n| 1 | 2 |"

COLUMNS_BLOCK = (
    "<columns>\n"
    '<column ratio="50">\n'
    "Gauche\n"
    "</column>\n"
    '<column ratio="50">\n'
    "Droite\n"
    "</column>\n"
    "</columns>"
)

EXPECTED_BLOCKS = [
    "# Titre",
    "Premier paragraphe\nsur deux lignes.",
    TABLE_BLOCK,
    COLUMNS_BLOCK,
    "- un",
    "- deux",
    "## Suite",
    "Dernier paragraphe.",
]

def test_read_page_paginates_by_blocks_and_resumes_with_next_offset(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stub = NotionStub()
        await stub.start()
        page = "f" * 32
        program_probe(stub)
        stub.route("GET", f"/v1/pages/{page}", body=_page(page, "Atelier"))
        stub.route(
            "GET",
            f"/v1/pages/{page}/markdown",
            body=_markdown_body(PAGINATED_MARKDOWN),
        )
        try:
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                first = await call_tool(
                    session, "read_page", {"page": page, "limit": 3}
                )
                second = await call_tool(
                    session,
                    "read_page",
                    {
                        "page": page,
                        "offset": first["next_offset"],
                        "limit": 3,
                    },
                )
                third = await call_tool(
                    session,
                    "read_page",
                    {"page": page, "offset": second["next_offset"]},
                )
        finally:
            await stub.stop()

        assert first["content"] == "\n\n".join(EXPECTED_BLOCKS[0:3])
        assert first["offset"] == 0
        assert first["next_offset"] == 3
        assert first["has_more"] is True
        assert first["total_blocks"] == 8

        # Chaque puce est un bloc de premier niveau : les deux puces comptent
        # séparément, et la tranche d'origine garde leur simple saut de ligne.
        assert second["content"] == COLUMNS_BLOCK + "\n\n- un\n- deux"
        assert second["offset"] == first["next_offset"]
        assert second["next_offset"] == 6
        assert second["has_more"] is True

        assert third["content"] == "\n\n".join(EXPECTED_BLOCKS[6:8])
        assert third["offset"] == second["next_offset"]
        assert third["next_offset"] == 8
        assert third["has_more"] is False

        # Les trois morceaux recollés rendent la page entière, sans perte.
        assert "\n\n".join(
            (first["content"], second["content"], third["content"])
        ) == PAGINATED_MARKDOWN.rstrip("\n")

    asyncio.run(scenario())

def test_read_page_never_splits_a_table_nor_a_columns_group(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stub = NotionStub()
        await stub.start()
        page = "f" * 32
        program_probe(stub)
        stub.route("GET", f"/v1/pages/{page}", body=_page(page, "Atelier"))
        stub.route(
            "GET",
            f"/v1/pages/{page}/markdown",
            body=_markdown_body(PAGINATED_MARKDOWN),
        )
        try:
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                one_by_one = []
                offset = 0
                while True:
                    payload = await call_tool(
                        session,
                        "read_page",
                        {"page": page, "offset": offset, "limit": 1},
                    )
                    one_by_one.append(payload["content"])
                    if not payload["has_more"]:
                        break
                    offset = payload["next_offset"]
        finally:
            await stub.stop()

        # Un bloc par appel : le tableau et le groupe de colonnes restent
        # chacun entiers, jamais coupés au milieu.
        assert one_by_one == EXPECTED_BLOCKS

    asyncio.run(scenario())


NESTED_LIST_MARKDOWN = (
    "- premier\n"
    "  continuation du premier\n"
    "  - imbriqué\n"
    "- deuxième\n"
    "1. numéro un\n"
    "2. numéro deux\n"
)


def test_read_page_counts_each_top_level_list_item_as_one_block(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "b9" * 16
            stub.route("GET", f"/v1/pages/{page}", body=_page(page, "Listes"))
            stub.route(
                "GET",
                f"/v1/pages/{page}/markdown",
                body=_markdown_body(NESTED_LIST_MARKDOWN),
            )
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                blocks = []
                offset = 0
                while True:
                    payload = await call_tool(
                        session,
                        "read_page",
                        {"page": page, "offset": offset, "limit": 1},
                    )
                    blocks.append(payload["content"])
                    if not payload["has_more"]:
                        break
                    offset = payload["next_offset"]
                total = payload["total_blocks"]

        assert total == 4
        assert blocks == [
            "- premier\n  continuation du premier\n  - imbriqué",
            "- deuxième",
            "1. numéro un",
            "2. numéro deux",
        ]

    asyncio.run(scenario())


def test_read_page_clamps_its_bounds_and_handles_an_offset_past_the_end(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        markdown = "\n\n".join(
            f"Paragraphe {index}" for index in range(60)
        )
        stub = NotionStub()
        await stub.start()
        page = "f" * 32
        program_probe(stub)
        stub.route("GET", f"/v1/pages/{page}", body=_page(page, "Atelier"))
        stub.route(
            "GET",
            f"/v1/pages/{page}/markdown",
            body=_markdown_body(markdown),
        )
        try:
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                defaulted = await call_tool(
                    session, "read_page", {"page": page}
                )
                clamped = await call_tool(
                    session,
                    "read_page",
                    {"page": page, "offset": -5, "limit": 999},
                )
                minimum = await call_tool(
                    session, "read_page", {"page": page, "limit": 0}
                )
                past_end = await call_tool(
                    session,
                    "read_page",
                    {"page": page, "offset": 500, "limit": 5},
                )
        finally:
            await stub.stop()

        assert defaulted["total_blocks"] == 60
        assert defaulted["next_offset"] == 20
        assert defaulted["has_more"] is True

        assert clamped["offset"] == 0
        assert clamped["next_offset"] == 50
        assert clamped["has_more"] is True

        assert minimum["content"] == "Paragraphe 0"
        assert minimum["next_offset"] == 1

        assert past_end["content"] == ""
        assert past_end["offset"] == 500
        assert past_end["next_offset"] == 500
        assert past_end["has_more"] is False
        assert past_end["total_blocks"] == 60

    asyncio.run(scenario())

def test_read_page_renders_non_empty_properties_as_text(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "a1" * 16
            stub.route(
                "GET",
                f"/v1/pages/{page}",
                body=_page(
                    page,
                    "Atelier",
                    properties={
                        "Statut": {
                            "type": "status",
                            "status": {"name": "En cours"},
                        },
                        "Étiquette": {
                            "type": "select",
                            "select": {"name": "Urgent"},
                        },
                        "Échéance": {
                            "type": "date",
                            "date": {"start": "2026-11-03"},
                        },
                        "Points": {"type": "number", "number": 3},
                        "Ratio": {"type": "number", "number": 3.5},
                        "Fait": {"type": "checkbox", "checkbox": True},
                        "Inactif": {"type": "checkbox", "checkbox": False},
                        "Lien": {
                            "type": "url",
                            "url": "https://example.com/atelier",
                        },
                        "Courriel": {
                            "type": "email",
                            "email": "atelier@example.com",
                        },
                        "Téléphone": {
                            "type": "phone_number",
                            "phone_number": "+33 1 23 45 67 89",
                        },
                        "Total": {
                            "type": "formula",
                            "formula": {"type": "number", "result": 7},
                        },
                        "Équipe": {
                            "type": "people",
                            "people": [{"name": "Ada"}, {"name": "Lin"}],
                        },
                        "Résumé": {
                            "type": "rich_text",
                            "rich_text": [
                                {"plain_text": "Un "},
                                {"plain_text": "résumé"},
                            ],
                        },
                        "Étiquettes": {
                            "type": "multi_select",
                            "multi_select": [
                                {"name": "Urgent"},
                                {"name": "Maison"},
                            ],
                        },
                        "Liens": {
                            "type": "relation",
                            "relation": [
                                {"id": "0" * 32},
                                {"id": "1" * 32},
                            ],
                        },
                        "Total roulé": {
                            "type": "rollup",
                            "rollup": {
                                "type": "number",
                                "function": "sum",
                                "number": 12,
                            },
                        },
                        "Fichiers": {
                            "type": "files",
                            "files": [
                                {"name": "plan.pdf"},
                                {"name": "notes.md"},
                            ],
                        },
                        "Identifiant": {
                            "type": "unique_id",
                            "unique_id": {"prefix": "A", "number": 4},
                        },
                        "Vide": {"type": "rich_text", "rich_text": []},
                        "Sans date": {"type": "date", "date": None},
                        "Aucune étiquette": {
                            "type": "multi_select",
                            "multi_select": [],
                        },
                        "Aucun lien": {"type": "relation", "relation": []},
                        "Aucun fichier": {"type": "files", "files": []},
                        "Roulé vide": {
                            "type": "rollup",
                            "rollup": {"type": "number", "number": None},
                        },
                    },
                ),
            )
            stub.route(
                "GET",
                f"/v1/pages/{page}/markdown",
                body=_markdown_body("Un paragraphe."),
            )
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "read_page", {"page": page}
                )

            properties = payload["page"]["properties"]
            assert properties == {
                "Titre": "Atelier",
                "Statut": "En cours",
                "Étiquette": "Urgent",
                "Échéance": "2026-11-03",
                "Points": "3",
                "Ratio": "3.5",
                "Fait": "oui",
                "Inactif": "non",
                "Lien": "https://example.com/atelier",
                "Courriel": "atelier@example.com",
                "Téléphone": "+33 1 23 45 67 89",
                "Total": "7",
                "Équipe": "Ada, Lin",
                "Résumé": "Un résumé",
                "Étiquettes": "Urgent, Maison",
                "Liens": ", ".join(["0" * 32, "1" * 32]),
                "Total roulé": "12",
                "Fichiers": "plan.pdf, notes.md",
                "Identifiant": "A-4",
            }
            # Ordre d'insertion de la page, propriétés vides et types inconnus
            # omis.
            assert list(properties) == [
                "Titre",
                "Statut",
                "Étiquette",
                "Échéance",
                "Points",
                "Ratio",
                "Fait",
                "Inactif",
                "Lien",
                "Courriel",
                "Téléphone",
                "Total",
                "Équipe",
                "Résumé",
                "Étiquettes",
                "Liens",
                "Total roulé",
                "Fichiers",
                "Identifiant",
            ]

    asyncio.run(scenario())

def test_read_page_passes_notion_truncation_flags_and_says_so(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "b2" * 16
            stub.route(
                "GET", f"/v1/pages/{page}", body=_page(page, "Longue")
            )
            stub.route(
                "GET",
                f"/v1/pages/{page}/markdown",
                body=_markdown_body(
                    "Un paragraphe.",
                    truncated=True,
                    unknown_block_ids=["bloc-1", "bloc-2"],
                ),
            )
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                truncated = await call_tool(
                    session, "read_page", {"page": page}
                )

                # Les drapeaux de Notion sont rendus tels quels.
                assert truncated["truncated"] is True
                assert truncated["unknown_block_ids"] == [
                    "bloc-1",
                    "bloc-2",
                ]
                assert "tronqué" in truncated["note"]
                assert "blocs" in truncated["note"]

                # Un drapeau seul suffit à poser la phrase.
                stub.route(
                    "GET",
                    f"/v1/pages/{page}/markdown",
                    body=_markdown_body("Un paragraphe.", truncated=True),
                )
                only_truncated = await call_tool(
                    session, "read_page", {"page": page}
                )
                assert "tronqué" in only_truncated["note"]
                assert only_truncated["unknown_block_ids"] == []

                # Page propre : aucun ajout, rien n'est coupé en silence.
                stub.route(
                    "GET",
                    f"/v1/pages/{page}/markdown",
                    body=_markdown_body("Un paragraphe."),
                )
                clean = await call_tool(
                    session, "read_page", {"page": page}
                )
                assert clean["truncated"] is False
                assert clean["unknown_block_ids"] == []
                assert "note" not in clean

    asyncio.run(scenario())

def test_a_missing_or_unshared_page_is_a_french_sentence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "c3" * 16
            sink = RecordingTraceSink()
            async with _notion_session(tmp_path, stub, sink=sink) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "read_page", {"page": page}
                )

                assert payload["status"] == "error"
                assert (
                    "Page introuvable ou non partagée avec la connexion"
                    in payload["message"]
                )
                # La page est demandée d'abord : pas de Markdown sans page.
                assert stub.requests_for("GET", f"/v1/pages/{page}")
                assert (
                    stub.requests_for(
                        "GET", f"/v1/pages/{page}/markdown"
                    )
                    == []
                )
                [trace] = sink.of_kind("ReadPage")
                assert trace.payload["status"] == "error"
                assert trace.payload["reason"] == "HTTP 404"
                assert trace.payload["blocks"] == 0
                assert trace.payload["chars"] == 0

    asyncio.run(scenario())

def test_an_invalid_page_identifier_makes_no_http_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            sink = RecordingTraceSink()
            async with _notion_session(tmp_path, stub, sink=sink) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                before = list(stub.requests)
                payload = await call_tool(
                    session, "read_page", {"page": "pas-un-identifiant"}
                )

                assert payload["status"] == "error"
                assert "invalide" in payload["message"]
                # Aucun appel HTTP : l'identifiant est refusé avant le réseau.
                assert stub.requests == before

    asyncio.run(scenario())

def test_read_page_accepts_a_notion_url_and_reduces_it_to_the_page_id(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "d4" * 16
            stub.route(
                "GET", f"/v1/pages/{page}", body=_page(page, "Atelier")
            )
            stub.route(
                "GET",
                f"/v1/pages/{page}/markdown",
                body=_markdown_body("Un paragraphe."),
            )
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "read_page",
                    {
                        "page": (
                            "https://www.notion.so/atelier/"
                            f"Atelier-{page}?pvs=4"
                        )
                    },
                )

            assert payload["status"] == "ok"
            assert payload["page"]["id"] == page
            assert stub.requests_for("GET", f"/v1/pages/{page}")

    asyncio.run(scenario())

def test_a_rate_limited_read_is_one_request_without_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "e5" * 16
            stub.route(
                "GET", f"/v1/pages/{page}", body=_page(page, "Atelier")
            )
            stub.route(
                "GET",
                f"/v1/pages/{page}/markdown",
                status=429,
                body={"object": "error", "status": 429},
            )
            sink = RecordingTraceSink()
            async with _notion_session(tmp_path, stub, sink=sink) as (
                app,
                _live,
                session,
                _,
                app_sink,
            ):
                payload = await call_tool(
                    session, "read_page", {"page": page}
                )

                assert payload["status"] == "error"
                assert "débit" in payload["message"]
                # Exactement une requête Markdown : aucun retry automatique.
                assert (
                    len(
                        stub.requests_for(
                            "GET", f"/v1/pages/{page}/markdown"
                        )
                    )
                    == 1
                )
                [trace] = sink.of_kind("ReadPage")
                assert trace.payload["reason"] == "HTTP 429"
                assert app.plugins.states["notion"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())

def test_an_unreadable_markdown_body_is_an_error_not_an_empty_page(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "f6" * 16
            stub.route(
                "GET", f"/v1/pages/{page}", body=_page(page, "Atelier")
            )
            sink = RecordingTraceSink()
            async with _notion_session(tmp_path, stub, sink=sink) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                stub.route(
                    "GET",
                    f"/v1/pages/{page}/markdown",
                    raw_body=b"{pas du json",
                )
                broken = await call_tool(
                    session, "read_page", {"page": page}
                )
                stub.route(
                    "GET",
                    f"/v1/pages/{page}/markdown",
                    body={"object": "page_markdown", "id": page},
                )
                shapeless = await call_tool(
                    session, "read_page", {"page": page}
                )

            assert broken["status"] == "error"
            assert "illisible" in broken["message"]
            assert shapeless["status"] == "error"
            assert "illisible" in shapeless["message"]
            assert [
                trace.payload["reason"] for trace in sink.of_kind("ReadPage")
            ] == ["unreadable body", "unreadable body"]

    asyncio.run(scenario())

def test_a_slow_notion_read_is_a_sentence_without_retry(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "a7" * 16
            stub.route(
                "GET", f"/v1/pages/{page}", body=_page(page, "Atelier")
            )
            stub.route(
                "GET",
                f"/v1/pages/{page}/markdown",
                delay=1.0,
                body=_markdown_body("Un paragraphe."),
            )
            sink = RecordingTraceSink()
            async with _notion_session(
                tmp_path, stub, sink=sink, timeout=0.2
            ) as (_, _live, session, _, _):
                payload = await call_tool(
                    session, "read_page", {"page": page}
                )

                assert payload["status"] == "error"
                assert "temps" in payload["message"]
                assert (
                    len(
                        stub.requests_for(
                            "GET", f"/v1/pages/{page}/markdown"
                        )
                    )
                    == 1
                )
                [trace] = sink.of_kind("ReadPage")
                assert trace.payload["reason"] == "timeout"

    asyncio.run(scenario())

def test_read_page_trace_carries_counts_and_never_content_or_token(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "b8" * 16
            stub.route(
                "GET", f"/v1/pages/{page}", body=_page(page, "Atelier")
            )
            stub.route(
                "GET",
                f"/v1/pages/{page}/markdown",
                body=_markdown_body(PAGINATED_MARKDOWN),
            )
            sink = RecordingTraceSink()
            async with _notion_session(tmp_path, stub, sink=sink) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "read_page",
                    {"page": page, "offset": 1, "limit": 2},
                )

            [trace] = sink.of_kind("ReadPage")
            # Pagination demandée, comptes rendus, durée et statut.
            assert trace.payload["page"] == page
            assert trace.payload["offset"] == 1
            assert trace.payload["limit"] == 2
            assert payload["content"] == "\n\n".join(EXPECTED_BLOCKS[1:3])
            assert trace.payload["blocks"] == 2
            assert trace.payload["chars"] == len(payload["content"])
            assert trace.payload["total_blocks"] == 8
            assert trace.payload["truncated"] is False
            assert trace.payload["unknown_block_ids"] == []
            assert isinstance(trace.payload["duration_ms"], (int, float))
            assert trace.payload["status"] == "ok"

            # Jamais le contenu, jamais le jeton.
            rendered = json.dumps(trace.payload)
            assert "Premier paragraphe" not in rendered
            assert "Dernier paragraphe" not in rendered
            assert SECRET not in rendered
            assert all(
                SECRET not in json.dumps(entry.payload, default=str)
                for entry in sink.entries
            )

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Filtre par mot
# ---------------------------------------------------------------------------


def test_read_page_declares_an_optional_query(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            async with _notion_session(tmp_path, stub) as (_, live, _, _, _):
                tools = declared(live)
                assert (
                    dict(tools["read_page"].parameters_json_schema)
                    == READ_PARAMETERS
                )
                # Le filtre est optionnel : la lecture paginée reste la norme.
                assert READ_PARAMETERS["required"] == ["page"]

    asyncio.run(scenario())

def test_read_page_without_query_keeps_the_paginated_reading(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "11" * 16
            markdown = "# Titre\n\nPremier.\n\nDeuxième.\n\nTroisième."
            await _served_page(stub, page, markdown)
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                first = await call_tool(
                    session, "read_page", {"page": page, "limit": 2}
                )
                second = await call_tool(
                    session,
                    "read_page",
                    {"page": page, "offset": first["next_offset"], "limit": 2},
                )

        assert first["content"] == "# Titre\n\nPremier."
        assert first["offset"] == 0
        assert first["next_offset"] == 2
        assert first["has_more"] is True
        assert first["total_blocks"] == 4
        # Sans filtre, la réponse reste celle de la lecture paginée.
        assert first["query"] is None
        assert first["match_count"] == 0

        assert second["content"] == "Deuxième.\n\nTroisième."
        assert second["has_more"] is False
        assert second["query"] is None
        assert second["match_count"] == 0

    asyncio.run(scenario())

ACCENT_MARKDOWN = (
    "# Été\n"
    "\n"
    "L'été a été chaud.\n"
    "\n"
    "Un article sur Notion.\n"
    "\n"
    "L'art du résumé.\n"
)

def test_query_matches_ignoring_case_and_accents_on_whole_words(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "22" * 16
            await _served_page(stub, page, ACCENT_MARKDOWN)
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                accents = await call_tool(
                    session, "read_page", {"page": page, "query": "ete"}
                )
                upper = await call_tool(
                    session, "read_page", {"page": page, "query": "ART"}
                )
                inside = await call_tool(
                    session, "read_page", {"page": page, "query": "resume"}
                )

        # « ete » retrouve « été » : accents ignorés.
        assert accents["status"] == "ok"
        assert accents["query"] == "ete"
        assert accents["match_count"] == 1
        assert "L'été a été chaud." in accents["content"]
        # Le texte rendu garde ses accents ; c'est la comparaison qui les ôte.
        assert "L'ete" not in accents["content"]
        assert "Un article" not in accents["content"]

        # « ART » retrouve « art » sans attraper « article ».
        assert upper["match_count"] == 1
        assert "L'art du résumé." in upper["content"]
        assert "article" not in upper["content"]

        # L'accent au milieu du mot tombe aussi.
        assert inside["match_count"] == 1
        assert "L'art du résumé." in inside["content"]

    asyncio.run(scenario())

PARAGRAPH_MARKDOWN = (
    "# Section avec art\n"
    "\n"
    "- art dans une puce\n"
    "\n"
    "| art | autre |\n"
    "| --- | --- |\n"
    "| 1 | 2 |\n"
    "\n"
    "```text\n"
    "art dans le code\n"
    "```\n"
    "\n"
    "<callout>\n"
    "art dans une balise\n"
    "</callout>\n"
    "\n"
    "Un paragraphe sans le mot.\n"
    "\n"
    "Le seul vrai art est ici.\n"
)

def test_query_never_matches_a_heading_a_list_a_table_or_a_code_block(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "33" * 16
            await _served_page(stub, page, PARAGRAPH_MARKDOWN)
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "read_page", {"page": page, "query": "art"}
                )

        # Un seul paragraphe contient le mot : ni le titre, ni la puce, ni le
        # tableau, ni le code, ni la balise ne sont des correspondances.
        assert payload["status"] == "ok"
        assert payload["match_count"] == 1
        assert "Le seul vrai art est ici." in payload["content"]
        assert "puce" not in payload["content"]
        assert "dans le code" not in payload["content"]
        assert "dans une balise" not in payload["content"]
        assert "| art | autre |" not in payload["content"]
        assert "Un paragraphe sans le mot." not in payload["content"]

    asyncio.run(scenario())


QUOTE_AND_BREAK_MARKDOWN = (
    "> art dans une citation\n"
    "> seconde ligne de citation\n"
    "\n"
    "---\n"
    "\n"
    "***\n"
    "\n"
    "___\n"
    "\n"
    "Le vrai paragraphe avec art.\n"
)


def test_query_never_matches_a_blockquote_nor_a_thematic_break(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "34" * 16
            await _served_page(stub, page, QUOTE_AND_BREAK_MARKDOWN)
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "read_page", {"page": page, "query": "art"}
                )

        # Une citation et un séparateur horizontal ne sont pas des
        # paragraphes : seul le vrai paragraphe est une correspondance.
        assert payload["status"] == "ok"
        assert payload["match_count"] == 1
        assert payload["content"] == "Le vrai paragraphe avec art."

    asyncio.run(scenario())


SECTION_MARKDOWN = (
    "Un paragraphe racine avant tout titre.\n"
    "\n"
    "# Section proche\n"
    "\n"
    "Le mot cible ici.\n"
    "\n"
    "## Section lointaine\n"
    "\n"
    "Un autre paragraphe.\n"
)

def test_query_renders_the_nearest_preceding_heading_only(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "44" * 16
            await _served_page(stub, page, SECTION_MARKDOWN)
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                inside_section = await call_tool(
                    session, "read_page", {"page": page, "query": "cible"}
                )
                before_titles = await call_tool(
                    session, "read_page", {"page": page, "query": "racine"}
                )

        # La correspondance est précédée du titre de sa section, et d'aucun
        # autre : la section suivante reste dehors.
        assert inside_section["content"] == (
            "# Section proche\n\nLe mot cible ici."
        )
        assert "Section lointaine" not in inside_section["content"]

        # Avant le premier titre, aucun contexte n'est inventé.
        assert before_titles["content"] == (
            "Un paragraphe racine avant tout titre."
        )

    asyncio.run(scenario())

MATCHES_MARKDOWN = (
    "# Section\n"
    "\n"
    "Cible 1.\n"
    "\n"
    "Sans correspondance.\n"
    "\n"
    "Cible 2.\n"
    "\n"
    "| Cible | autre |\n"
    "| --- | --- |\n"
    "| 3 | 4 |\n"
    "\n"
    "Cible 3.\n"
    "\n"
    "Cible 4.\n"
    "\n"
    "Cible 5.\n"
)

def test_query_paginates_matches_not_blocks(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "55" * 16
            await _served_page(stub, page, MATCHES_MARKDOWN)
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payloads = []
                offset = 0
                while True:
                    payload = await call_tool(
                        session,
                        "read_page",
                        {"page": page, "query": "cible", "offset": offset,
                         "limit": 2},
                    )
                    payloads.append(payload)
                    if not payload["has_more"]:
                        break
                    offset = payload["next_offset"]

        first, second, third = payloads
        # `offset` et `next_offset` comptent les correspondances, pas les
        # blocs.
        assert [payload["offset"] for payload in payloads] == [0, 2, 4]
        assert [payload["next_offset"] for payload in payloads] == [2, 4, 5]
        assert [payload["has_more"] for payload in payloads] == [
            True,
            True,
            False,
        ]
        assert [payload["match_count"] for payload in payloads] == [5, 5, 5]
        # `total_blocks` reste le compte de blocs de la page, tableau compris.
        assert [payload["total_blocks"] for payload in payloads] == [8, 8, 8]

        # Aucun recouvrement, aucun trou : chaque correspondance une seule
        # fois.
        collected = "\n\n".join(
            payload["content"] for payload in payloads
        )
        for index in range(1, 6):
            assert collected.count(f"Cible {index}.") == 1
        assert "Sans correspondance" not in collected
        assert first["content"].endswith("Cible 2.")
        assert second["content"].endswith("Cible 4.")
        assert third["content"].endswith("Cible 5.")

    asyncio.run(scenario())

def test_a_query_without_match_is_an_empty_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "66" * 16
            await _served_page(stub, page, MATCHES_MARKDOWN)
            sink = RecordingTraceSink()
            async with _notion_session(tmp_path, stub, sink=sink) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session,
                    "read_page",
                    {"page": page, "query": "introuvable"},
                )

        # Aucune correspondance n'est jamais une erreur.
        assert payload["status"] == "ok"
        assert payload["content"] == ""
        assert payload["match_count"] == 0
        assert payload["has_more"] is False
        assert payload["next_offset"] == 0
        assert payload["total_blocks"] == 8
        [trace] = sink.of_kind("ReadPage")
        assert trace.payload["status"] == "ok"
        assert trace.payload["match_count"] == 0

    asyncio.run(scenario())

def test_query_stays_within_the_requested_page(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "77" * 16
            other = "88" * 16
            await _served_page(stub, page, "Le mot cible ici.")
            await _served_page(stub, other, "Le mot cible ailleurs.")
            async with _notion_session(tmp_path, stub) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "read_page", {"page": page, "query": "cible"}
                )

                assert payload["content"] == "Le mot cible ici."
                # Un seul Markdown lu, celui de la page demandée : ni l'autre
                # page, ni une recherche de workspace.
                assert stub.requests_for("GET", f"/v1/pages/{page}/markdown")
                assert (
                    stub.requests_for("GET", f"/v1/pages/{other}")
                    == []
                )
                assert (
                    stub.requests_for("GET", f"/v1/pages/{other}/markdown")
                    == []
                )
                assert stub.requests_for("POST", "/v1/search") == []

    asyncio.run(scenario())

def test_read_page_trace_carries_the_query_and_its_match_count(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "99" * 16
            await _served_page(stub, page, MATCHES_MARKDOWN)
            sink = RecordingTraceSink()
            async with _notion_session(tmp_path, stub, sink=sink) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                await call_tool(
                    session, "read_page", {"page": page, "query": "cible"}
                )
                await call_tool(session, "read_page", {"page": page})

            filtered, plain = sink.of_kind("ReadPage")
            assert filtered.payload["query"] == "cible"
            assert filtered.payload["match_count"] == 5
            # Jamais le contenu, jamais le jeton.
            rendered = json.dumps(filtered.payload)
            assert "Cible 1." not in rendered
            assert SECRET not in rendered
            assert all(
                SECRET not in json.dumps(entry.payload, default=str)
                for entry in sink.entries
            )
            # Sans filtre, la Trace reste celle de la lecture paginée.
            assert "query" not in plain.payload
            assert "match_count" not in plain.payload

    asyncio.run(scenario())

def test_a_missing_page_with_a_query_is_still_the_standard_sentence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = "aa" * 16
            sink = RecordingTraceSink()
            async with _notion_session(tmp_path, stub, sink=sink) as (
                _,
                _live,
                session,
                _,
                _,
            ):
                payload = await call_tool(
                    session, "read_page", {"page": page, "query": "cible"}
                )

        # Un 404 reste un 404 : jamais « zéro correspondance ».
        assert payload["status"] == "error"
        assert payload["message"] == (
            "Page introuvable ou non partagée avec la connexion Notion."
        )
        assert stub.requests_for("GET", f"/v1/pages/{page}")
        assert stub.requests_for("GET", f"/v1/pages/{page}/markdown") == []
        [trace] = sink.of_kind("ReadPage")
        assert trace.payload["status"] == "error"
        assert trace.payload["reason"] == "HTTP 404"
        assert trace.payload["match_count"] == 0

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Création
# ---------------------------------------------------------------------------


def test_create_page_is_declared_with_its_exact_schema(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                # Le Tool est visible dans l'instantané de la Console.
                snapshot = app.snapshot()["plugins"]
                assert len(snapshot) == 1
                [notion] = snapshot
                assert notion["name"] == "notion"
                assert notion["state"] == "activated"

                async with live_session(transport, live):
                    tools = declared(live)
                    assert dict(
                        tools["create_page"].parameters_json_schema
                    ) == CREATE_PARAMETERS
                    description = tools["create_page"].description.lower()
                    assert "crée une page" in description

    asyncio.run(scenario())

def test_create_page_uses_the_configured_parent_and_sends_an_explicit_title(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page_id = "e" * 32
            stub.route(
                "POST", "/v1/pages", body=_page(page_id, "Idée")
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "create_page", {"title": "Idée"}
                    )

                assert payload == {
                    "status": "ok",
                    "page": {
                        "id": page_id,
                        "title": "Idée",
                        "url": f"https://www.notion.so/{page_id}",
                    },
                }

                # Sans ``parent``, la page naît sous la page configurée, et la
                # propriété de titre part explicitement.
                [request] = stub.requests_for("POST", "/v1/pages")
                assert request["authorization"] == f"Bearer {SECRET}"
                assert request["notion_version"] == "2026-03-11"
                assert request["body"] == {
                    "parent": {
                        "type": "page_id",
                        "page_id": DEFAULT_PARENT,
                    },
                    "properties": {
                        "title": {
                            "type": "title",
                            "title": [
                                {
                                    "type": "text",
                                    "text": {"content": "Idée"},
                                }
                            ],
                        }
                    },
                }
                assert "allow_async" not in request["body"]
                assert "children" not in request["body"]

    asyncio.run(scenario())

def test_create_page_honours_a_named_parent_given_by_id_or_url(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            parent_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
            parent_url = "https://www.notion.so/atelier/Parent-" + "b" * 32
            stub.route(
                "POST",
                "/v1/pages",
                body=_page("c" * 32, "Note"),
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    dashed = await call_tool(
                        session,
                        "create_page",
                        {
                            "title": "Note",
                            "parent": (
                                "a1b2c3d4-e5f6-0718-293a-4b5c6d7e8f90"
                            ),
                        },
                    )
                    url = await call_tool(
                        session,
                        "create_page",
                        {"title": "Note", "parent": parent_url},
                    )
                    empty = await call_tool(
                        session,
                        "create_page",
                        {"title": "Note", "parent": ""},
                    )

                assert dashed["status"] == "ok"
                assert url["status"] == "ok"
                assert empty["status"] == "ok"
                bodies = [
                    request["body"]
                    for request in stub.requests_for("POST", "/v1/pages")
                ]
                # Identifiant et URL sont réduits à la même forme sans tirets ;
                # un parent vide retombe sur la page configurée.
                assert [body["parent"]["page_id"] for body in bodies] == [
                    parent_id,
                    "b" * 32,
                    DEFAULT_PARENT,
                ]
                assert all(
                    body["parent"]["type"] == "page_id" for body in bodies
                )

    asyncio.run(scenario())

def test_create_page_with_empty_content_creates_a_title_only_page(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page_id = "d" * 32
            # Notion peut rendre une page dont la propriété de titre est vide :
            # le titre demandé sert alors de repli.
            stub.route(
                "POST",
                "/v1/pages",
                body={"object": "page", "id": page_id, "url": ""},
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    empty = await call_tool(
                        session,
                        "create_page",
                        {"title": "Page vide", "content": ""},
                    )
                    absent = await call_tool(
                        session, "create_page", {"title": "Sans corps"}
                    )

                assert empty == {
                    "status": "ok",
                    "page": {
                        "id": page_id,
                        "title": "Page vide",
                        "url": "",
                    },
                }
                assert absent["status"] == "ok"
                bodies = [
                    request["body"]
                    for request in stub.requests_for("POST", "/v1/pages")
                ]
                assert len(bodies) == 2
                for body in bodies:
                    # Ni corps ni version : une version seule est un 400.
                    assert "markdown" not in body
                    assert "markdown_version" not in body

    asyncio.run(scenario())

def test_create_page_returns_the_created_page_identity_and_title(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page_id = "9" * 32
            created_url = (
                "https://www.notion.so/atelier/Note-finale-" + page_id
            )
            # Le serveur fait autorité : son titre remplace celui demandé.
            stub.route(
                "POST",
                "/v1/pages",
                body=_page(
                    page_id, "Note finale", url=created_url
                ),
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "create_page",
                        {"title": "brouillon"},
                    )

                assert payload == {
                    "status": "ok",
                    "page": {
                        "id": page_id,
                        "title": "Note finale",
                        "url": created_url,
                    },
                }

    asyncio.run(scenario())

def test_create_page_sends_markdown_v2_next_to_the_title_property(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page_id = "f" * 32
            content = "# Plan\n\n- premier point\n- second point"
            stub.route(
                "POST", "/v1/pages", body=_page(page_id, "Plan")
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "create_page",
                        {"title": "Plan", "content": content},
                    )

                assert payload["status"] == "ok"
                [request] = stub.requests_for("POST", "/v1/pages")
                assert request["body"] == {
                    "parent": {
                        "type": "page_id",
                        "page_id": DEFAULT_PARENT,
                    },
                    "properties": {
                        "title": {
                            "type": "title",
                            "title": [
                                {
                                    "type": "text",
                                    "text": {"content": "Plan"},
                                }
                            ],
                        }
                    },
                    "markdown": content,
                    "markdown_version": "v2",
                }
                # La version Markdown accompagne le corps, juste à côté.
                assert list(request["body"]) == [
                    "parent",
                    "properties",
                    "markdown",
                    "markdown_version",
                ]
                assert "allow_async" not in request["body"]
                assert "children" not in request["body"]

                [create] = sink.of_kind("CreatePage")
                assert create.payload["parent"] == DEFAULT_PARENT
                assert create.payload["page"] == page_id
                assert create.payload["title_chars"] == len("Plan")
                assert create.payload["content_chars"] == len(content)
                assert create.payload["status"] == "ok"
                assert isinstance(create.payload["duration_ms"], (int, float))
                # Jamais le contenu, jamais le jeton.
                rendered = json.dumps(create.payload, ensure_ascii=False)
                assert "premier point" not in rendered
                assert SECRET not in rendered

    asyncio.run(scenario())

def test_a_refused_markdown_is_a_french_sentence_and_creates_nothing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            content = "| a | b |\n| - | - |\n| 1 | 2 |"
            stub.route(
                "POST",
                "/v1/pages",
                status=400,
                body={"object": "error", "status": 400},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "create_page",
                        {"title": "Tableau", "content": content},
                    )

                assert payload["status"] == "error"
                assert "Markdown" in payload["message"]
                assert "5 000 blocs" in payload["message"]
                assert "taille" in payload["message"]
                # Un refus ne crée rien et n'est jamais rejoué.
                assert len(stub.requests_for("POST", "/v1/pages")) == 1
                [create] = sink.of_kind("CreatePage")
                assert create.payload["status"] == "error"
                assert create.payload["reason"] == "HTTP 400"
                assert create.payload["content_chars"] == len(content)

    asyncio.run(scenario())

def test_an_unshared_parent_page_is_the_standard_sentence_and_creates_nothing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "POST",
                "/v1/pages",
                status=404,
                body={"object": "error", "status": 404},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "create_page",
                        {"title": "Orpheline"},
                    )

                assert payload == {
                    "status": "error",
                    "message": (
                        "Page introuvable ou non partagée avec la connexion "
                        "Notion."
                    ),
                }
                assert len(stub.requests_for("POST", "/v1/pages")) == 1
                [create] = sink.of_kind("CreatePage")
                assert create.payload["status"] == "error"
                assert create.payload["reason"] == "HTTP 404"

    asyncio.run(scenario())

@pytest.mark.parametrize(
    ("status", "fragment"),
    [
        (401, "ATHENA_PLUGIN_NOTION_TOKEN"),
        (403, "capacité"),
        (429, "débit"),
        (529, "débit"),
        (500, "500"),
        (503, "peut avoir été enregistré"),
        (504, "peut avoir été enregistré"),
    ],
)
def test_create_status_failures_render_french_sentences_without_retrying(
    tmp_path: Path, status: int, fragment: str
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "POST",
                "/v1/pages",
                status=status,
                body={"object": "error", "status": status},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "create_page",
                        {"title": "En échec"},
                    )

                assert payload["status"] == "error"
                assert fragment in payload["message"]
                if status in (503, 504):
                    # Une écriture incertaine invite à relire avant de réessayer
                    assert "relis" in payload["message"].lower()
                # Exactement une requête : aucun retry automatique.
                assert len(stub.requests_for("POST", "/v1/pages")) == 1
                [create] = sink.of_kind("CreatePage")
                assert create.payload["status"] == "error"
                assert create.payload["reason"] == f"HTTP {status}"
                assert app.plugins.states["notion"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())

def test_an_invalid_parent_renders_a_french_sentence_without_any_request(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    before = len(stub.requests)
                    payload = await call_tool(
                        session,
                        "create_page",
                        {"title": "Ailleurs", "parent": "pas-une-page"},
                    )

                assert payload["status"] == "error"
                assert "identifiant" in payload["message"].lower()
                # Aucun appel HTTP : l'identifiant est refusé avant le réseau.
                assert len(stub.requests) == before
                assert stub.requests_for("POST", "/v1/pages") == []
                [create] = sink.of_kind("CreatePage")
                assert create.payload["status"] == "error"
                assert create.payload["reason"] == "invalid parent"
                assert create.payload["parent"] is None

    asyncio.run(scenario())

def test_create_page_timeout_and_unreadable_body_are_french_errors(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "POST",
                "/v1/pages",
                delay=1.0,
                body=_page("8" * 32, "Trop tard"),
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(
                tmp_path, sink, stub.base_url, timeout=0.2
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    timed_out = await call_tool(
                        session,
                        "create_page",
                        {"title": "Trop tard"},
                    )
                    stub.route(
                        "POST", "/v1/pages", raw_body=b"{pas du json"
                    )
                    broken = await call_tool(
                        session,
                        "create_page",
                        {"title": "Cassée"},
                    )

                assert timed_out["status"] == "error"
                assert "temps" in timed_out["message"]
                assert broken["status"] == "error"
                assert "illisible" in broken["message"]
                assert len(stub.requests_for("POST", "/v1/pages")) == 2
                assert [
                    entry.payload["reason"]
                    for entry in sink.of_kind("CreatePage")
                ] == ["timeout", "unreadable body"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Ajout et renommage
# ---------------------------------------------------------------------------


def test_update_page_is_declared_with_append_default_and_confirm(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live):
                    tools = declared(live)

                spec = tools["update_page"]
                assert dict(spec.parameters_json_schema) == UPDATE_PARAMETERS
                # Le mode est optionnel et vaut append : la déclaration ne
                # l'exige pas, la description l'annonce.
                assert "append" in spec.parameters_json_schema["properties"][
                    "mode"
                ]["description"]
                assert "Ajoute du contenu" in spec.description

    asyncio.run(scenario())

def test_append_inserts_content_at_the_end_and_leaves_existing_content(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                body={
                    "object": "page_markdown",
                    "id": TARGET,
                    "markdown": "déjà là\n\nAjout",
                },
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "content": "Ajout"},
                    )

                assert payload == {
                    "status": "ok",
                    "page": _summary(page),
                    "mode": "append",
                    "title_updated": False,
                    "content_updated": True,
                    "warnings": [],
                }
                [request] = stub.requests_for(
                    "PATCH", f"/v1/pages/{TARGET}/markdown"
                )
                assert request["authorization"] == f"Bearer {SECRET}"
                assert request["notion_version"] == "2026-03-11"
                assert request["body"] == _append_body("Ajout")
                # Aucun remplacement, aucune écriture asynchrone : seul un
                # ajout en fin de page a été demandé.
                assert "replace_content" not in json.dumps(request["body"])
                assert "allow_async" not in json.dumps(request["body"])
                assert stub.requests_for("PATCH", f"/v1/pages/{TARGET}") == []

    asyncio.run(scenario())

def test_rename_patches_the_discovered_title_property_in_its_own_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            renamed = _page(TARGET, "Atelier corrigé", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route("PATCH", f"/v1/pages/{TARGET}", body=renamed)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "title": "Atelier corrigé"},
                    )

                assert payload == {
                    "status": "ok",
                    "page": _summary(renamed),
                    "mode": "append",
                    "title_updated": True,
                    "content_updated": False,
                    "warnings": [],
                }
                [request] = stub.requests_for("PATCH", f"/v1/pages/{TARGET}")
                # La clé découverte est « Name », pas « title ».
                assert request["body"] == _rename_body("Name", "Atelier corrigé")
                # Le corps n'a pas été touché : aucun appel Markdown.
                assert (
                    stub.requests_for(
                        "PATCH", f"/v1/pages/{TARGET}/markdown"
                    )
                    == []
                )

    asyncio.run(scenario())

def test_title_and_body_are_two_writes_in_a_deterministic_order(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Brouillon", key="Name")
            renamed = _page(TARGET, "Version finale", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route("PATCH", f"/v1/pages/{TARGET}", body=renamed)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                body={"object": "page_markdown", "id": TARGET},
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "title": "Version finale",
                            "content": "Conclusion",
                        },
                    )

                assert payload == {
                    "status": "ok",
                    "page": _summary(renamed),
                    "mode": "append",
                    "title_updated": True,
                    "content_updated": True,
                    "warnings": [],
                }
                writes = _writes(stub)
                assert [request["path"] for request in writes] == [
                    f"/v1/pages/{TARGET}",
                    f"/v1/pages/{TARGET}/markdown",
                ]
                assert writes[0]["body"] == _rename_body(
                    "Name", "Version finale"
                )
                assert writes[1]["body"] == _append_body("Conclusion")

    asyncio.run(scenario())

def test_partial_success_names_what_worked_and_what_failed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Brouillon", key="Name")
            renamed = _page(TARGET, "Version finale", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route("PATCH", f"/v1/pages/{TARGET}", body=renamed)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                status=400,
                body={"object": "error", "status": 400},
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "title": "Version finale",
                            "content": "Conclusion",
                        },
                    )

                assert payload["status"] == "error"
                assert payload["page"] == _summary(renamed)
                assert payload["mode"] == "append"
                assert payload["title_updated"] is True
                assert payload["content_updated"] is False
                # La phrase nomme le titre réussi, le corps échoué, et invite
                # à relire avant de réessayer.
                assert "titre" in payload["message"]
                assert "contenu" in payload["message"]
                assert "Relis la page avant de réessayer" in payload["message"]
                # Les deux écritures ont bien été tentées, une seule fois.
                assert len(
                    stub.requests_for("PATCH", f"/v1/pages/{TARGET}")
                ) == 1
                assert len(
                    stub.requests_for(
                        "PATCH", f"/v1/pages/{TARGET}/markdown"
                    )
                ) == 1

    asyncio.run(scenario())

def test_partial_success_in_the_other_direction_is_also_named(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Brouillon", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}",
                status=403,
                body={"object": "error", "status": 403},
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                body={"object": "page_markdown", "id": TARGET},
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "title": "Version finale",
                            "content": "Conclusion",
                        },
                    )

                assert payload["status"] == "error"
                assert payload["title_updated"] is False
                assert payload["content_updated"] is True
                assert "contenu" in payload["message"]
                assert "titre" in payload["message"]
                assert "Relis la page avant de réessayer" in payload["message"]

    asyncio.run(scenario())

def test_notion_warnings_are_transmitted_and_counted_in_the_trace(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            warnings = [
                {
                    "code": "parser_change",
                    "message": "Some blocks were read as paragraphs.",
                }
            ]
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                body={
                    "object": "page_markdown",
                    "id": TARGET,
                    "warnings": warnings,
                },
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "content": "Ajout"},
                    )

                assert payload["warnings"] == warnings
                [trace] = sink.of_kind("UpdatePage")
                # La Trace porte les codes Notion, jamais les messages qui
                # peuvent citer le contenu de la page.
                assert trace.payload["warnings"] == ["parser_change"]
                assert trace.payload["warnings_count"] == 1
                assert "Some blocks were read as paragraphs." not in json.dumps(
                    trace.payload
                )

    asyncio.run(scenario())

def test_a_503_on_a_write_warns_it_may_have_been_recorded_and_is_not_retried(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                status=503,
                body={"object": "error", "status": 503},
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}",
                status=504,
                body={"object": "error", "status": 504},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    appended = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "content": "Ajout"},
                    )
                    renamed = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "title": "Nouveau"},
                    )

                for payload in (appended, renamed):
                    assert payload["status"] == "error"
                    assert "peut avoir été enregistré" in payload["message"]
                    assert "relis la page avant de réessayer" in (
                        payload["message"]
                    )
                # Une écriture incertaine n'est jamais rejouée : une seule
                # requête par action.
                assert len(
                    stub.requests_for(
                        "PATCH", f"/v1/pages/{TARGET}/markdown"
                    )
                ) == 1
                assert len(
                    stub.requests_for("PATCH", f"/v1/pages/{TARGET}")
                ) == 1
                assert [
                    entry.payload["reason"] for entry in sink.of_kind("UpdatePage")
                ] == ["HTTP 503", "HTTP 504"]

    asyncio.run(scenario())

def test_mode_replace_is_refused_without_confirmation_and_without_any_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    without = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "content": "Tout", "mode": "replace"},
                    )

                assert without["status"] == "error"
                assert "remplacement" in without["message"]
                assert "confirmation" in without["message"]
                # Sans confirmation, aucune écriture n'est tentée.
                assert _writes(stub) == []

    asyncio.run(scenario())

def test_an_invalid_page_id_is_a_french_sentence_without_any_http_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    before = len(stub.requests)
                    payload = await call_tool(
                        session,
                        "update_page",
                        {"page": "pas-une-page", "content": "Ajout"},
                    )

                assert payload["status"] == "error"
                assert "identifiant" in payload["message"]
                assert len(stub.requests) == before

    asyncio.run(scenario())

def test_an_unshared_page_is_a_french_sentence_and_never_a_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            # Aucune route pour la page cible : le faux serveur répond 404.
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "content": "Ajout"},
                    )

                assert payload["status"] == "error"
                assert "introuvable ou non partagée avec la connexion" in (
                    payload["message"]
                )
                assert _writes(stub) == []

    asyncio.run(scenario())

def test_neither_title_nor_content_writes_nothing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "update_page", {"page": TARGET}
                    )

                assert payload == {
                    "status": "ok",
                    "page": _summary(page),
                    "mode": "append",
                    "title_updated": False,
                    "content_updated": False,
                    "warnings": [],
                }
                assert _writes(stub) == []

    asyncio.run(scenario())

def test_update_page_trace_carries_counts_and_never_the_content_or_token(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        content = "Un texte privé ajouté"
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                body={"object": "page_markdown", "id": TARGET},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                app_sink,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "content": content},
                    )

                assert payload["status"] == "ok"
                [trace] = sink.of_kind("UpdatePage")
                assert trace.payload["page"] == TARGET
                assert trace.payload["mode"] == "append"
                assert trace.payload["title_updated"] is False
                assert trace.payload["content_updated"] is True
                assert trace.payload["chars"] == len(content)
                assert trace.payload["warnings"] == []
                assert trace.payload["warnings_count"] == 0
                assert trace.payload["status"] == "ok"
                assert isinstance(trace.payload["duration_ms"], (int, float))
                traces = json.dumps(
                    [entry.payload for entry in (*sink.entries, *app_sink.entries)],
                    default=str,
                )
                assert content not in traces
                assert SECRET not in traces

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Remplacement protégé
# ---------------------------------------------------------------------------


REFUSED = (
    "Le remplacement du contenu exige une confirmation explicite. "
    "Relance l'appel avec confirm=true, ou utilise le mode append pour "
    "ajouter du contenu."
)

NOTION_REFUSED = (
    "Notion a refusé le remplacement du contenu. Le plus souvent, ce refus "
    "protège une sous-page ou une base enfant que le remplacement aurait "
    "supprimée : elle n'a pas été touchée. Un Markdown invalide ou une page "
    "trop volumineuse provoquent le même refus. Rien n'a été perdu et le "
    "corps d'origine est intact."
)

def test_replace_without_confirmation_is_refused_with_zero_http_calls(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    # Les appels de la sonde sont derrière nous : le refus ne
                    # doit produire aucune requête, pas même une lecture.
                    stub.requests.clear()
                    payload = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "content": "Tout", "mode": "replace"},
                    )

                assert payload == {"status": "error", "message": REFUSED}
                assert stub.requests == []
                [trace] = sink.of_kind("UpdatePage")
                assert trace.payload["mode"] == "replace"
                assert trace.payload["status"] == "error"
                assert trace.payload["reason"] == "replace refused"

    asyncio.run(scenario())

def test_replace_with_confirmation_patches_replace_content_without_delete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                body={"object": "page_markdown", "id": TARGET},
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "content": "Corps remplacé",
                            "mode": "replace",
                            "confirm": True,
                        },
                    )

                assert payload == {
                    "status": "ok",
                    "page": _summary(page),
                    "mode": "replace",
                    "title_updated": False,
                    "content_updated": True,
                    "warnings": [],
                }
                [request] = stub.requests_for(
                    "PATCH", f"/v1/pages/{TARGET}/markdown"
                )
                assert request["authorization"] == f"Bearer {SECRET}"
                assert request["notion_version"] == "2026-03-11"
                body = request["body"]
                assert body == _replace_body("Corps remplacé")
                assert body["replace_content"]["allow_deleting_content"] is False
                assert body["markdown_version"] == "v2"
                assert "allow_async" not in body
                assert "position" not in body["replace_content"]
                assert "insert_content" not in body
                assert _writes(stub) == [request]

    asyncio.run(scenario())

def test_replace_without_content_is_refused_without_http(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    stub.requests.clear()
                    payload = await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "mode": "replace", "confirm": True},
                    )

                assert payload["status"] == "error"
                assert "contenu" in payload["message"]
                assert "remplacement" in payload["message"]
                assert stub.requests == []

    asyncio.run(scenario())

def test_replace_that_would_delete_a_child_page_is_refused_and_nothing_lost(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        original = "Corps d'origine\n\nDeuxième paragraphe"
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                status=400,
                body={
                    "object": "error",
                    "status": 400,
                    "code": "validation_error",
                    "message": "Cannot delete child page while replacing.",
                },
            )
            stub.route(
                "GET",
                f"/v1/pages/{TARGET}/markdown",
                body={
                    "object": "page_markdown",
                    "id": TARGET,
                    "markdown": original,
                    "truncated": False,
                    "unknown_block_ids": [],
                },
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "content": "Nouveau corps",
                            "mode": "replace",
                            "confirm": True,
                        },
                    )
                    read = await call_tool(
                        session, "read_page", {"page": TARGET}
                    )

                assert payload["status"] == "error"
                assert payload["message"] == NOTION_REFUSED
                # La protection des enfants reste le cas de sécurité nommé,
                # sans exclure les autres causes d'un 400.
                assert "sous-page" in payload["message"]
                assert "base enfant" in payload["message"]
                assert "Markdown invalide" in payload["message"]
                assert "trop volumineuse" in payload["message"]
                assert "Rien n'a été perdu" in payload["message"]
                # Une seule tentative de remplacement, aucun second écrit.
                assert len(
                    stub.requests_for(
                        "PATCH", f"/v1/pages/{TARGET}/markdown"
                    )
                ) == 1
                assert len(_writes(stub)) == 1
                # La lecture suivante rend toujours le corps d'origine.
                assert read["status"] == "ok"
                assert read["content"] == original

    asyncio.run(scenario())

def test_partial_success_when_rename_works_but_replace_fails(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Brouillon", key="Name")
            renamed = _page(TARGET, "Version finale", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route("PATCH", f"/v1/pages/{TARGET}", body=renamed)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                status=503,
                body={"object": "error", "status": 503},
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "title": "Version finale",
                            "content": "Nouveau corps",
                            "mode": "replace",
                            "confirm": True,
                        },
                    )

                assert payload["status"] == "error"
                assert payload["page"] == _summary(renamed)
                assert payload["mode"] == "replace"
                assert payload["title_updated"] is True
                assert payload["content_updated"] is False
                assert "titre" in payload["message"]
                assert "contenu" in payload["message"]
                assert "remplacé" in payload["message"]
                assert "relis la page avant de réessayer" in (
                    payload["message"].lower()
                )
                writes = _writes(stub)
                assert [request["path"] for request in writes] == [
                    f"/v1/pages/{TARGET}",
                    f"/v1/pages/{TARGET}/markdown",
                ]
                assert writes[0]["body"] == _rename_body(
                    "Name", "Version finale"
                )
                assert writes[1]["body"] == _replace_body("Nouveau corps")

    asyncio.run(scenario())

def test_partial_success_when_replace_works_but_rename_fails(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        warnings = [
            {
                "code": "parser_change",
                "message": "Some blocks were read as paragraphs.",
            }
        ]
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Brouillon", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}",
                status=403,
                body={"object": "error", "status": 403},
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                body={
                    "object": "page_markdown",
                    "id": TARGET,
                    "warnings": warnings,
                },
            )
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "title": "Version finale",
                            "content": "Nouveau corps",
                            "mode": "replace",
                            "confirm": True,
                        },
                    )

                assert payload["status"] == "error"
                assert payload["page"] == _summary(page)
                assert payload["mode"] == "replace"
                assert payload["title_updated"] is False
                assert payload["content_updated"] is True
                assert payload["warnings"] == warnings
                assert "titre" in payload["message"]
                assert "contenu" in payload["message"]
                assert "remplacé" in payload["message"]
                assert "Relis la page avant de réessayer" in payload["message"]

    asyncio.run(scenario())

def test_replace_warnings_are_transmitted_and_counted_in_the_trace(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        warnings = [
            {"code": "unknown_block", "message": "A block was skipped."}
        ]
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                body={
                    "object": "page_markdown",
                    "id": TARGET,
                    "warnings": warnings,
                },
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "content": "Nouveau corps",
                            "mode": "replace",
                            "confirm": True,
                        },
                    )

                assert payload["warnings"] == warnings
                [trace] = sink.of_kind("UpdatePage")
                assert trace.payload["mode"] == "replace"
                assert trace.payload["warnings"] == ["unknown_block"]
                assert trace.payload["warnings_count"] == 1
                assert "A block was skipped." not in json.dumps(trace.payload)
                assert trace.payload["status"] == "ok"

    asyncio.run(scenario())

def test_a_503_or_504_on_replace_is_uncertain_and_never_retried(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route_sequence(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                (
                    {"status": 503, "body": {"object": "error"}},
                    {"status": 504, "body": {"object": "error"}},
                ),
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payloads = []
                    for _ in range(2):
                        payloads.append(
                            await call_tool(
                                session,
                                "update_page",
                                {
                                    "page": TARGET,
                                    "content": "Nouveau corps",
                                    "mode": "replace",
                                    "confirm": True,
                                },
                            )
                        )

                for payload in payloads:
                    assert payload["status"] == "error"
                    assert "peut avoir été enregistré" in payload["message"]
                    assert "relis la page avant de réessayer" in (
                        payload["message"]
                    )
                # Une écriture incertaine n'est jamais rejouée.
                assert len(
                    stub.requests_for(
                        "PATCH", f"/v1/pages/{TARGET}/markdown"
                    )
                ) == 2
                assert [
                    entry.payload["reason"]
                    for entry in sink.of_kind("UpdatePage")
                ] == ["HTTP 503", "HTTP 504"]

    asyncio.run(scenario())

def test_a_404_on_replace_is_the_standard_sentence_and_never_a_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            # Aucune route pour la page cible : le faux serveur répond 404.
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "content": "Nouveau corps",
                            "mode": "replace",
                            "confirm": True,
                        },
                    )

                assert payload == {
                    "status": "error",
                    "message": (
                        "Page introuvable ou non partagée avec la connexion "
                        "Notion."
                    ),
                }
                assert _writes(stub) == []

    asyncio.run(scenario())

def test_trace_distinguishes_replace_from_append_and_never_leaks(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        content = "Un texte privé remplacé"
        async with NotionStub() as stub:
            program_probe(stub)
            page = _page(TARGET, "Atelier", key="Name")
            stub.route("GET", f"/v1/pages/{TARGET}", body=page)
            stub.route(
                "PATCH",
                f"/v1/pages/{TARGET}/markdown",
                body={"object": "page_markdown", "id": TARGET},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                app_sink,
            ):
                async with live_session(transport, live) as session:
                    await call_tool(
                        session,
                        "update_page",
                        {"page": TARGET, "content": content},
                    )
                    await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "content": content,
                            "mode": "replace",
                            "confirm": True,
                        },
                    )

                traces = sink.of_kind("UpdatePage")
                assert [trace.payload["mode"] for trace in traces] == [
                    "append",
                    "replace",
                ]
                replaced = traces[1]
                assert replaced.payload["page"] == TARGET
                assert replaced.payload["title_updated"] is False
                assert replaced.payload["content_updated"] is True
                assert replaced.payload["chars"] == len(content)
                assert replaced.payload["warnings"] == []
                assert replaced.payload["warnings_count"] == 0
                assert replaced.payload["status"] == "ok"
                assert isinstance(replaced.payload["duration_ms"], (int, float))
                rendered = json.dumps(
                    [
                        entry.payload
                        for entry in (*sink.entries, *app_sink.entries)
                    ],
                    default=str,
                )
                assert content not in rendered
                assert SECRET not in rendered

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Corbeille et restauration
# ---------------------------------------------------------------------------


def test_trash_and_restore_declare_their_exact_schemas(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live):
                    tools = declared(live)

                assert (
                    dict(tools["trash_page"].parameters_json_schema)
                    == TRASH_PARAMETERS
                )
                assert (
                    dict(tools["restore_page"].parameters_json_schema)
                    == RESTORE_PARAMETERS
                )
                assert "corbeille" in tools["trash_page"].description
                assert "corbeille" in tools["restore_page"].description

    asyncio.run(scenario())

def test_child_pages_are_counted_across_a_paginated_walk_and_trashed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route("GET", _page_path(), body=_page(PAGE_ID))
            # Deux pages de blocs : le compte doit suivre le curseur.
            stub.route_sequence(
                "GET",
                _children_path(),
                [
                    {
                        "body": _children(
                            "child_page", "paragraph", has_more=True,
                            cursor="curseur-2",
                        )
                    },
                    {
                        "body": _children(
                            "child_page", "child_page", "heading_2"
                        )
                    },
                ],
            )
            stub.route("PATCH", _page_path(), body=_page(PAGE_ID))
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "trash_page",
                        {"page": PAGE_ID, "confirm": True},
                    )

                # Trois sous-pages, dont une sur la seconde page de blocs.
                assert payload == {
                    "status": "ok",
                    "page": {
                        "id": PAGE_ID,
                        "title": "Journal de bord",
                        "url": f"https://www.notion.so/{PAGE_ID}",
                    },
                    "child_pages": 3,
                    "restorable": True,
                }

                # Le corps de l'écriture est exact.
                [write] = stub.requests_for("PATCH", _page_path())
                assert write["body"] == {"in_trash": True}
                assert write["authorization"] == f"Bearer {SECRET}"
                assert write["notion_version"] == "2026-03-11"

                first, second = stub.requests_for("GET", _children_path())
                # Requête exacte : taille 100, puis le curseur rendu par Notion.
                assert first["query"] == {"page_size": ["100"]}
                assert second["query"] == {
                    "page_size": ["100"],
                    "start_cursor": ["curseur-2"],
                }

                [trash] = sink.of_kind("TrashPage")
                assert trash.payload["page"] == PAGE_ID
                assert trash.payload["child_pages"] == 3
                assert trash.payload["status"] == "ok"
                assert "reason" not in trash.payload
                assert isinstance(trash.payload["duration_ms"], (int, float))
                assert SECRET not in json.dumps(
                    [entry.payload for entry in sink.entries], default=str
                )

    asyncio.run(scenario())

def test_trash_without_confirmation_writes_nothing_but_announces_the_page(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route("GET", _page_path(), body=_page(PAGE_ID))
            stub.route(
                "GET",
                _children_path(),
                body=_children("child_page", "paragraph", "child_page"),
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "trash_page", {"page": PAGE_ID}
                    )

                # Le refus annonce quand même la page et ses sous-pages.
                assert payload["status"] == "error"
                assert "Journal de bord" in payload["message"]
                assert "corbeille" in payload["message"]
                assert payload["page"] == {
                    "id": PAGE_ID,
                    "title": "Journal de bord",
                    "url": f"https://www.notion.so/{PAGE_ID}",
                }
                assert payload["child_pages"] == 2
                # Aucune requête d'écriture : la lecture seule est permise.
                assert _writes(stub) == []

                [trash] = sink.of_kind("TrashPage")
                assert trash.payload["page"] == PAGE_ID
                assert trash.payload["child_pages"] == 2
                assert trash.payload["status"] == "error"
                assert trash.payload["reason"] == "confirmation"
                assert isinstance(trash.payload["duration_ms"], (int, float))

    asyncio.run(scenario())

def test_an_invalid_page_identifier_is_a_sentence_without_any_http_call(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    before = len(stub.requests)
                    trashed = await call_tool(
                        session, "trash_page", {"page": "pas-une-page"}
                    )
                    restored = await call_tool(
                        session, "restore_page", {"page": "toujours pas"}
                    )

                assert trashed["status"] == "error"
                assert "identifiant" in trashed["message"]
                assert restored["status"] == "error"
                assert "identifiant" in restored["message"]
                # Aucun appel HTTP : l'identifiant est refusé avant le réseau.
                assert len(stub.requests) == before
                assert [
                    entry.payload["reason"]
                    for entry in sink.of_kind("TrashPage")
                ] == ["invalid page"]
                assert [
                    entry.payload["reason"]
                    for entry in sink.of_kind("RestorePage")
                ] == ["invalid page"]

    asyncio.run(scenario())

def test_an_unshared_page_returns_the_standard_sentence_without_writing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "GET",
                _page_path(),
                status=404,
                body={"object": "error", "status": 404},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "trash_page",
                        {"page": PAGE_ID, "confirm": True},
                    )

                assert payload["status"] == "error"
                assert payload["message"] == (
                    "Page introuvable ou non partagée avec la connexion Notion."
                )
                # La page est lue, jamais écrite.
                assert stub.requests_for("PATCH", _page_path()) == []
                assert _writes(stub) == []
                [trash] = sink.of_kind("TrashPage")
                assert trash.payload["status"] == "error"
                assert trash.payload["reason"] == "HTTP 404"
                assert trash.payload["page"] == PAGE_ID

    asyncio.run(scenario())

@pytest.mark.parametrize("status", [503, 504])
def test_a_5xx_on_the_trash_warns_the_change_may_have_been_recorded(
    tmp_path: Path, status: int
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route("GET", _page_path(), body=_page(PAGE_ID))
            stub.route("GET", _children_path(), body=_children())
            stub.route(
                "PATCH",
                _page_path(),
                status=status,
                body={"object": "error", "status": status},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                app_sink,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session,
                        "trash_page",
                        {"page": PAGE_ID, "confirm": True},
                    )

                assert payload["status"] == "error"
                assert "peut avoir été enregistré" in payload["message"]
                assert "relis" in payload["message"]
                # Un seul essai : l'écriture incertaine n'est jamais rejouée.
                assert len(stub.requests_for("PATCH", _page_path())) == 1
                trace = sink.of_kind("TrashPage")[-1].payload
                assert trace["status"] == "error"
                assert trace["reason"] == f"HTTP {status}"
                # Le compte connu accompagne l'échec d'écriture.
                assert trace["child_pages"] == 0
                assert app.plugins.states["notion"] == "activated"
                assert not app_sink.of_kind("SessionClosed")

    asyncio.run(scenario())

def test_restore_sends_in_trash_false_and_returns_the_page(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route("PATCH", _page_path(), body=_page(PAGE_ID))
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "restore_page", {"page": PAGE_ID}
                    )

                assert payload == {
                    "status": "ok",
                    "page": {
                        "id": PAGE_ID,
                        "title": "Journal de bord",
                        "url": f"https://www.notion.so/{PAGE_ID}",
                    },
                }
                [write] = stub.requests_for("PATCH", _page_path())
                assert write["body"] == {"in_trash": False}
                # La réponse portait déjà la page : aucune relecture inutile.
                assert stub.requests_for("GET", _page_path()) == []
                [restore] = sink.of_kind("RestorePage")
                assert restore.payload["page"] == PAGE_ID
                assert restore.payload["status"] == "ok"
                assert "child_pages" not in restore.payload
                assert isinstance(restore.payload["duration_ms"], (int, float))
                assert SECRET not in json.dumps(
                    [entry.payload for entry in sink.entries], default=str
                )

    asyncio.run(scenario())

def test_restore_rereads_the_page_when_the_write_response_lacks_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "PATCH", _page_path(), body={"object": "page", "id": PAGE_ID}
            )
            stub.route("GET", _page_path(), body=_page(PAGE_ID, title="Ressuscitée"))
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "restore_page", {"page": PAGE_ID}
                    )

                assert payload["page"]["title"] == "Ressuscitée"
                assert payload["page"]["url"] == (
                    f"https://www.notion.so/{PAGE_ID}"
                )
                assert len(stub.requests_for("PATCH", _page_path())) == 1
                # La sonde a déjà lu la page parente : une seule relecture ici.
                assert len(stub.requests_for("GET", _page_path())) == 1

    asyncio.run(scenario())

@pytest.mark.parametrize("status", [503, 504])
def test_a_5xx_on_the_restore_is_flagged_as_uncertain(
    tmp_path: Path, status: int
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "PATCH",
                _page_path(),
                status=status,
                body={"object": "error", "status": status},
            )
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, "restore_page", {"page": PAGE_ID}
                    )

                assert payload["status"] == "error"
                assert "peut avoir été enregistré" in payload["message"]
                assert len(stub.requests_for("PATCH", _page_path())) == 1
                trace = sink.of_kind("RestorePage")[-1].payload
                assert trace["reason"] == f"HTTP {status}"
                assert trace["status"] == "error"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Tableaux et colonnes
# ---------------------------------------------------------------------------


PIPE_TABLE = (
    "| Nom | Rôle |\n"
    "| --- | --- |\n"
    "| Ada | Poète |"
)

STYLED_TABLE = (
    '<table fit-page-width="true" header-row="true" header-column="false">\n'
    "<colgroup>\n"
    '<col width="180" color="gray" />\n'
    "</colgroup>\n"
    "\n"
    '<tr color="default">\n'
    '<th color="blue_bg">Nom</th>\n'
    '<td color="default">\n'
    "Ada\n"
    "\n"
    "Poète\n"
    "</td>\n"
    "</tr>\n"
    "</table>"
)

COLUMNS = (
    "<columns>\n"
    '<column ratio="50">\n'
    "Gauche\n"
    "</column>\n"
    '<column ratio="50">\n'
    "Droite\n"
    "</column>\n"
    "</columns>"
)

NESTED_COLUMNS = (
    "<columns>\n"
    '<column ratio="60">\n'
    "Gauche\n"
    "\n"
    "<columns>\n"
    '<column ratio="50">\n'
    "Haut\n"
    "</column>\n"
    '<column ratio="50">\n'
    "Bas\n"
    "</column>\n"
    "</columns>\n"
    "</column>\n"
    '<column ratio="40">\n'
    "Droite\n"
    "</column>\n"
    "</columns>"
)

PASSTHROUGH_TAGS = (
    '<callout icon="💡" color="yellow_bg">\nAstuce\n</callout>',
    '<toggle heading="h2">\nDétail\n</toggle>',
    (
        '<page url="https://www.notion.so/'
        '0123456789abcdef0123456789abcdef">Sous-page</page>'
    ),
    (
        '<database url="https://www.notion.so/'
        '0123456789abcdef0123456789abcdef" inline="false">Base</database>'
    ),
    (
        '<synced-block url="https://www.notion.so/'
        '0123456789abcdef0123456789abcdef">\n'
        "Contenu synchronisé\n"
        "</synced-block>"
    ),
)

V2_BLOCKS = [
    "# Atelier",
    "Une introduction.",
    PIPE_TABLE,
    STYLED_TABLE,
    COLUMNS,
    NESTED_COLUMNS,
    *PASSTHROUGH_TAGS,
    "- un",
    "- deux",
]

V2_PAGE = "\n".join(V2_BLOCKS)

TABLE_INDEX = V2_BLOCKS.index(PIPE_TABLE)

COLUMNS_INDEX = V2_BLOCKS.index(COLUMNS)

NESTED_INDEX = V2_BLOCKS.index(NESTED_COLUMNS)

def _table_only(stub: NotionStub, markdown: str) -> None:
    """Programme une page dont le corps est exactement ``markdown``."""
    program_probe(stub)
    stub.route("GET", f"/v1/pages/{MD_PAGE}", body=_page(MD_PAGE, "Atelier"))
    stub.route(
        "GET",
        f"/v1/pages/{MD_PAGE}/markdown",
        body=_markdown_body(markdown),
    )

def test_create_page_sends_a_pipe_table_verbatim_with_markdown_v2(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "POST", "/v1/pages", body=_page(MD_PAGE, "Notes")
            )
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                payload = await call_tool(
                    session,
                    "create_page",
                    {"title": "Notes", "content": PIPE_TABLE},
                )

            assert payload["status"] == "ok"
            [request] = stub.requests_for("POST", "/v1/pages")
            body = request["body"]
            # Le tableau part verbatim : aucune conversion côté client.
            assert body["markdown"] == PIPE_TABLE
            assert body["markdown_version"] == "v2"
            assert list(body) == [
                "parent",
                "properties",
                "markdown",
                "markdown_version",
            ]
            assert "children" not in body
            assert len(_writes(stub)) == 1

    asyncio.run(scenario())

def test_create_page_sends_a_columns_block_verbatim(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "POST", "/v1/pages", body=_page(MD_PAGE, "Colonnes")
            )
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                payload = await call_tool(
                    session,
                    "create_page",
                    {"title": "Colonnes", "content": COLUMNS},
                )

            assert payload["status"] == "ok"
            [request] = stub.requests_for("POST", "/v1/pages")
            # La syntaxe documentée part telle quelle, ratio compris.
            assert request["body"]["markdown"] == COLUMNS
            assert request["body"]["markdown_version"] == "v2"
            assert len(_writes(stub)) == 1

    asyncio.run(scenario())

def test_update_page_appends_a_pipe_table_verbatim_with_markdown_v2(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "GET", f"/v1/pages/{MD_PAGE}", body=_page(MD_PAGE, "Atelier")
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{MD_PAGE}/markdown",
                body=_markdown_body(PIPE_TABLE),
            )
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                payload = await call_tool(
                    session,
                    "update_page",
                    {"page": MD_PAGE, "content": PIPE_TABLE},
                )

            assert payload["status"] == "ok"
            assert payload["content_updated"] is True
            [request] = stub.requests_for(
                "PATCH", f"/v1/pages/{MD_PAGE}/markdown"
            )
            assert request["body"] == {
                "type": "insert_content",
                "insert_content": {
                    "content": PIPE_TABLE,
                    "position": {"type": "end"},
                },
                "markdown_version": "v2",
            }
            assert len(_writes(stub)) == 1

    asyncio.run(scenario())

def test_update_page_appends_a_columns_block_verbatim(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "GET", f"/v1/pages/{MD_PAGE}", body=_page(MD_PAGE, "Atelier")
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{MD_PAGE}/markdown",
                body=_markdown_body(COLUMNS),
            )
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                payload = await call_tool(
                    session,
                    "update_page",
                    {"page": MD_PAGE, "content": COLUMNS},
                )

            assert payload["status"] == "ok"
            [request] = stub.requests_for(
                "PATCH", f"/v1/pages/{MD_PAGE}/markdown"
            )
            assert request["body"]["insert_content"]["content"] == COLUMNS
            assert request["body"]["markdown_version"] == "v2"
            assert len(_writes(stub)) == 1

    asyncio.run(scenario())

def test_read_page_returns_a_pipe_table_verbatim(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            _table_only(stub, PIPE_TABLE)
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                payload = await call_tool(
                    session, "read_page", {"page": MD_PAGE}
                )

            assert payload["status"] == "ok"
            # Notion a rendu une pipe table : elle ressort identique.
            assert payload["content"] == PIPE_TABLE
            assert payload["total_blocks"] == 1
            [request] = stub.requests_for(
                "GET", f"/v1/pages/{MD_PAGE}/markdown"
            )
            assert request["query"] == {"markdown_version": ["v2"]}

    asyncio.run(scenario())

def test_read_page_returns_a_styled_table_verbatim(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            _table_only(stub, STYLED_TABLE)
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                payload = await call_tool(
                    session, "read_page", {"page": MD_PAGE}
                )

            assert payload["status"] == "ok"
            # Un tableau stylé sort en balises ``table``, sans réécriture.
            assert payload["content"] == STYLED_TABLE
            assert payload["total_blocks"] == 1

    asyncio.run(scenario())

def test_read_page_transmits_notion_tags_verbatim(tmp_path: Path) -> None:
    async def scenario() -> None:
        markdown = "\n".join(PASSTHROUGH_TAGS)
        async with NotionStub() as stub:
            _table_only(stub, markdown)
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                payload = await call_tool(
                    session, "read_page", {"page": MD_PAGE}
                )

            assert payload["status"] == "ok"
            # callout, toggle, page, database et synced-block : tels quels.
            assert payload["content"] == markdown
            assert payload["total_blocks"] == len(PASSTHROUGH_TAGS)

    asyncio.run(scenario())

def test_read_page_keeps_a_table_and_columns_whole_at_every_offset(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            _table_only(stub, V2_PAGE)
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                chunks = []
                offsets = []
                offset = 0
                while True:
                    payload = await call_tool(
                        session,
                        "read_page",
                        {"page": MD_PAGE, "offset": offset, "limit": 1},
                    )
                    offsets.append(payload["offset"])
                    chunks.append(payload["content"])
                    if not payload["has_more"]:
                        break
                    offset = payload["next_offset"]

        # Un bloc par appel : le tableau et le groupe de colonnes restent
        # chacun entiers, jamais coupés au milieu.
        assert chunks == V2_BLOCKS
        assert offsets == list(range(len(V2_BLOCKS)))
        assert chunks[TABLE_INDEX] == PIPE_TABLE
        assert chunks[COLUMNS_INDEX] == COLUMNS
        assert chunks[NESTED_INDEX] == NESTED_COLUMNS

        # Recoller les tranches rend le Markdown de Notion, séparateurs
        # compris : la lecture ne réécrit pas la page.
        assert "\n".join(chunks) == V2_PAGE

    asyncio.run(scenario())

def test_read_page_never_cuts_a_table_nor_columns_at_a_mid_page_offset(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            _table_only(stub, V2_PAGE)
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                around_table = await call_tool(
                    session,
                    "read_page",
                    {"page": MD_PAGE, "offset": TABLE_INDEX - 1, "limit": 2},
                )
                around_columns = await call_tool(
                    session,
                    "read_page",
                    {"page": MD_PAGE, "offset": COLUMNS_INDEX, "limit": 3},
                )
                table_alone = await call_tool(
                    session,
                    "read_page",
                    {"page": MD_PAGE, "offset": TABLE_INDEX, "limit": 1},
                )
                around_nested = await call_tool(
                    session,
                    "read_page",
                    {"page": MD_PAGE, "offset": NESTED_INDEX - 1, "limit": 2},
                )

        assert around_table["content"] == (
            V2_BLOCKS[TABLE_INDEX - 1] + "\n" + PIPE_TABLE
        )
        # Le groupe apparaît entier, une seule fois, sans fragment.
        assert around_table["content"].count(PIPE_TABLE) == 1
        assert around_table["content"].count("| --- | --- |") == 1
        assert around_table["content"].count("| Ada | Poète |") == 1

        assert around_columns["content"] == "\n".join(
            V2_BLOCKS[COLUMNS_INDEX : COLUMNS_INDEX + 3]
        )
        # Le groupe apparaît entier, une seule fois, sans fragment.
        assert around_columns["content"].count(COLUMNS) == 1
        assert around_columns["content"].startswith(COLUMNS + "\n")

        assert table_alone["content"] == PIPE_TABLE

        # Une colonne qui contient un groupe imbriqué reste entière elle aussi.
        assert around_nested["content"] == (
            V2_BLOCKS[NESTED_INDEX - 1] + "\n" + NESTED_COLUMNS
        )
        assert around_nested["content"].count(NESTED_COLUMNS) == 1

    asyncio.run(scenario())

def test_markdown_v2_is_explicit_on_read_create_and_append(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            _table_only(stub, PIPE_TABLE)
            stub.route(
                "POST", "/v1/pages", body=_page(MD_PAGE, "Tableau")
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{MD_PAGE}/markdown",
                body=_markdown_body(PIPE_TABLE),
            )
            async with _notion_session(tmp_path, stub) as (_, _, session, _, _):
                read = await call_tool(
                    session, "read_page", {"page": MD_PAGE}
                )
                created = await call_tool(
                    session,
                    "create_page",
                    {"title": "Tableau", "content": PIPE_TABLE},
                )
                appended = await call_tool(
                    session,
                    "update_page",
                    {"page": MD_PAGE, "content": PIPE_TABLE},
                )

        assert read["status"] == "ok"
        assert created["status"] == "ok"
        assert appended["status"] == "ok"

        [read_request] = stub.requests_for(
            "GET", f"/v1/pages/{MD_PAGE}/markdown"
        )
        [create_request] = stub.requests_for("POST", "/v1/pages")
        [append_request] = stub.requests_for(
            "PATCH", f"/v1/pages/{MD_PAGE}/markdown"
        )
        # Lecture : la version est un paramètre de requête explicite.
        assert read_request["query"] == {"markdown_version": ["v2"]}
        # Création : elle est dans le corps, juste à côté de ``markdown``.
        assert create_request["body"]["markdown_version"] == "v2"
        assert "markdown" in create_request["body"]
        # Ajout : elle est dans le corps, au même niveau que la commande.
        assert append_request["body"]["markdown_version"] == "v2"

    asyncio.run(scenario())

def test_invalid_v2_markdown_on_create_is_one_french_sentence_and_no_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        invalid = "<columns><column ratio=\"50\">manquante"
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "POST",
                "/v1/pages",
                status=400,
                body={
                    "object": "error",
                    "status": 400,
                    "code": "validation_error",
                    "message": "The request body was invalid.",
                },
            )
            async with _notion_session(tmp_path, stub) as (_, _, session, sink, _):
                payload = await call_tool(
                    session,
                    "create_page",
                    {"title": "Cassée", "content": invalid},
                )

                assert payload["status"] == "error"
                assert "Markdown" in payload["message"]
                assert "5 000 blocs" in payload["message"]
                # Un seul essai, aucune écriture partielle.
                assert len(stub.requests_for("POST", "/v1/pages")) == 1
                assert len(_writes(stub)) == 1
                [create] = sink.of_kind("CreatePage")
                assert create.payload["status"] == "error"
                assert create.payload["reason"] == "HTTP 400"

    asyncio.run(scenario())

def test_invalid_v2_markdown_on_append_is_one_french_sentence_and_no_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        invalid = "<table><tr><td>cellule"
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "GET", f"/v1/pages/{MD_PAGE}", body=_page(MD_PAGE, "Atelier")
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{MD_PAGE}/markdown",
                status=400,
                body={
                    "object": "error",
                    "status": 400,
                    "code": "validation_error",
                    "message": "The request body was invalid.",
                },
            )
            async with _notion_session(tmp_path, stub) as (_, _, session, sink, _):
                payload = await call_tool(
                    session,
                    "update_page",
                    {"page": MD_PAGE, "content": invalid},
                )

                assert payload["status"] == "error"
                assert "refusé" in payload["message"]
                assert "contenu invalide" in payload["message"]
                # Exactement une écriture, jamais rejouée, rien d'autre.
                assert (
                    len(
                        stub.requests_for(
                            "PATCH", f"/v1/pages/{MD_PAGE}/markdown"
                        )
                    )
                    == 1
                )
                assert stub.requests_for(
                    "PATCH", f"/v1/pages/{MD_PAGE}"
                ) == []
                assert len(_writes(stub)) == 1
                [update] = sink.of_kind("UpdatePage")
                assert update.payload["status"] == "error"
                assert update.payload["reason"] == "HTTP 400"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Vérification transverse
# ---------------------------------------------------------------------------


def _markdown(content: str = "Bonjour le monde") -> dict:
    return {
        "object": "page_markdown",
        "id": PAGE,
        "markdown": content,
        "truncated": False,
        "unknown_block_ids": [],
    }

def _assert_french_error(payload: dict) -> None:
    """Une phrase française rendue, jamais une exception échappée."""
    assert payload["status"] == "error"
    message = payload["message"]
    assert isinstance(message, str) and message.strip()
    # ``ToolRegistry`` préfixe par « tool … » une exception qui s'échappe.
    assert not message.startswith("tool ")

def test_the_six_tools_have_their_exact_schemas_and_console_row(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live):
                    tools = declared(live)

                assert sorted(tools) == TOOL_NAMES
                for name in TOOL_NAMES:
                    assert (
                        dict(tools[name].parameters_json_schema)
                        == PARAMETERS[name]
                    ), name
                    assert tools[name].description == DESCRIPTIONS[name], name

                [notion] = app.snapshot()["plugins"]
                assert notion["name"] == "notion"
                assert notion["state"] == "activated"
                assert notion["tools"] == TOOL_NAMES
                assert notion["declaration"]["api_version"] == 1

    asyncio.run(scenario())

def test_the_instruction_reaches_the_system_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                _,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live):
                    # Les Tools sont bien déclarés, puis l'instruction les décrit.
                    assert sorted(declared(live)) == TOOL_NAMES
                    assert INSTRUCTION in live.instruction()

    asyncio.run(scenario())

def test_an_announcement_carries_the_instruction_without_any_tool(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            sink = RecordingTraceSink()
            registry, loader = build_plugins(tmp_path, sink, stub.base_url)
            async with running_app(
                registry=registry,
                loader=loader,
                announce_prompt="ANNONCE",
                judge=FakeJudge(message="La porte est ouverte"),
            ) as (app, live, transport, app_sink):
                assert app.plugins.states["notion"] == "activated"
                # Les Tools existent : c'est le gate de l'Annonce qui les retire.
                assert app.tools.owner_of("search_pages") == "notion"

                async with websockets.connect(url(transport)) as ws:
                    await register(
                        ws, transport, client_name="desktop", drain=False
                    )
                    await wait_until(
                        lambda: any(
                            entry.kind == "ClientRegistered"
                            for entry in app_sink.entries
                        )
                    )
                    app.push_event("matrix", "porte ouverte")
                    # Fenêtre de regroupement de 5 s avant l'Annonce (ADR-0007).
                    await wait_until(
                        lambda: bool(app_sink.of_kind("EventJudge")),
                        timeout=7.0,
                    )

                # L'instruction reste dans le prompt, aucun Tool n'est déclaré.
                assert INSTRUCTION in live.instruction()
                assert "ANNONCE" in live.instruction()
                assert live.declarations() == []
                assert app.plugins.states["notion"] == "activated"

    asyncio.run(scenario())

def test_the_token_never_leaks_while_tool_traces_do(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "server.log"
    log = FileTraceSink(log_path, min_level="debug")
    try:

        async def scenario() -> None:
            async with NotionStub() as stub:
                program_probe(stub)
                stub.route(
                    "POST",
                    "/v1/search",
                    body={
                        "object": "list",
                        "results": [_page("d" * 32, "Secrète")],
                        "has_more": False,
                        "next_cursor": None,
                    },
                )
                stub.route("GET", f"/v1/pages/{PAGE}", body=_page(PAGE))
                stub.route(
                    "GET", f"/v1/pages/{PAGE}/markdown", body=_markdown()
                )
                plugin_sink = RecordingTraceSink()
                registry, loader = build_plugins(
                    tmp_path,
                    plugin_sink,
                    stub.base_url,
                    extra_sinks=[log],
                )
                async with running_app(
                    registry=registry, loader=loader, extra_sinks=[log]
                ) as (app, live, transport, app_sink):
                    async with live_session(transport, live) as session:
                        search = await call_tool(
                            session, "search_pages", {"query": "page secrète"}
                        )
                        read = await call_tool(
                            session, "read_page", {"page": PAGE}
                        )

                    assert search["status"] == "ok"
                    assert read["status"] == "ok"
                    # Le jeton part réellement au serveur : le test n'est pas vide.
                    assert set(
                        request["authorization"] for request in stub.requests
                    ) == {f"Bearer {SECRET}"}

                    traces = [
                        entry.payload
                        for entry in (*app_sink.entries, *plugin_sink.entries)
                    ]
                    assert SECRET not in json.dumps(traces, default=str)
                    assert SECRET not in json.dumps(
                        app.snapshot(), default=str
                    )
                    assert SECRET not in json.dumps(
                        app.plugins.states, default=str
                    )
                    assert plugin_sink.of_kind("Search")
                    assert plugin_sink.of_kind("ReadPage")

        asyncio.run(scenario())
    finally:
        log.close()

    rendered = log_path.read_text(encoding="utf-8")
    # Le journal porte les Traces de Tool : l'absence de jeton est réelle.
    assert "Search" in rendered
    assert "ReadPage" in rendered
    assert SECRET not in rendered

FIRST_REQUESTS: dict[str, tuple[dict, str, str]] = {
    "search_pages": (
        {"query": "journal"},
        "POST",
        "/v1/search",
    ),
    "read_page": (
        {"page": PAGE},
        "GET",
        f"/v1/pages/{PAGE}",
    ),
    "create_page": (
        {"title": "Note"},
        "POST",
        "/v1/pages",
    ),
    "update_page": (
        # Ajout : le premier appel est la lecture de la page.
        {"page": PAGE, "content": "Suite"},
        "GET",
        f"/v1/pages/{PAGE}",
    ),
    "trash_page": (
        # Confirmé : le premier appel reste la lecture de la page.
        {"page": PAGE, "confirm": True},
        "GET",
        f"/v1/pages/{PAGE}",
    ),
    "restore_page": (
        {"page": PAGE},
        "PATCH",
        f"/v1/pages/{PAGE}",
    ),
}

@pytest.mark.parametrize("tool", sorted(FIRST_REQUESTS))
def test_a_429_makes_exactly_one_request_and_never_retries(
    tmp_path: Path, tool: str
) -> None:
    arguments, method, path = FIRST_REQUESTS[tool]

    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    before = len(stub.requests)
                    stub.route(
                        method,
                        path,
                        status=429,
                        body={"object": "error", "status": 429},
                    )
                    payload = await call_tool(session, tool, arguments)

                # Une seule requête pour cet appel : rien n'est rejoué.
                requests = stub.requests[before:]
                assert len(requests) == 1
                assert requests[0]["method"] == method
                assert requests[0]["path"] == path

                _assert_french_error(payload)
                assert "débit" in payload["message"]
                assert app.plugins.states["notion"] == "activated"

    asyncio.run(scenario())

WRITES: dict[str, tuple[dict, str, str]] = {
    "create": (
        {"title": "Note", "content": "Un corps"},
        "POST",
        "/v1/pages",
    ),
    "append": (
        {"page": PAGE, "content": "Suite"},
        "PATCH",
        f"/v1/pages/{PAGE}/markdown",
    ),
    "replace": (
        {
            "page": PAGE,
            "content": "Nouveau corps",
            "mode": "replace",
            "confirm": True,
        },
        "PATCH",
        f"/v1/pages/{PAGE}/markdown",
    ),
    "trash": (
        {"page": PAGE, "confirm": True},
        "PATCH",
        f"/v1/pages/{PAGE}",
    ),
    "restore": (
        {"page": PAGE},
        "PATCH",
        f"/v1/pages/{PAGE}",
    ),
}

WRITE_TOOL: dict[str, str] = {
    "create": "create_page",
    "append": "update_page",
    "replace": "update_page",
    "trash": "trash_page",
    "restore": "restore_page",
}

def _program_write(stub: NotionStub, write: str, status: int) -> None:
    """Programme les lectures nécessaires, puis l'échec de la seule écriture."""
    if write in ("append", "replace", "trash"):
        stub.route("GET", f"/v1/pages/{PAGE}", body=_page(PAGE))
    if write == "trash":
        stub.route(
            "GET", f"/v1/blocks/{PAGE}/children", body=_children()
        )
    _, method, path = WRITES[write]
    stub.route(method, path, status=status, body={"object": "error"})

@pytest.mark.parametrize("write", sorted(WRITES))
@pytest.mark.parametrize("status", [503, 504])
def test_a_5xx_on_a_write_is_uncertain_and_attempted_once(
    tmp_path: Path, write: str, status: int
) -> None:
    arguments, method, path = WRITES[write]

    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            _program_write(stub, write, status)
            registry, loader = build_plugins(
                tmp_path, RecordingTraceSink(), stub.base_url
            )
            async with running_app(registry=registry, loader=loader) as (
                app,
                live,
                transport,
                _,
            ):
                async with live_session(transport, live) as session:
                    payload = await call_tool(
                        session, WRITE_TOOL[write], arguments
                    )

                _assert_french_error(payload)
                assert "peut avoir été enregistré" in payload["message"]
                assert "relis" in payload["message"].lower()
                # L'écriture a été tentée une seule fois, jamais rejouée.
                assert len(stub.requests_for(method, path)) == 1
                assert app.plugins.states["notion"] == "activated"

    asyncio.run(scenario())
