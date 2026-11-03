"""Vérification transverse du plugin ``notion`` (ticket #120).

Chaque test traverse la couture réelle : App → faux Gemini Live → registre →
plugin copié → faux serveur Notion. Rien n'est simulé en dehors du modèle et de
la REST : les Tools, l'instruction, l'Annonce, les Traces et le journal rendu
s'observent à la frontière.

Le test de l'instruction définit sa propre copie du texte : une dérive entre le
plugin et l'usage promis échoue ici, comme pour ``websearch``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import websockets

from conftest import FakeJudge, RecordingTraceSink, _url, register, wait_until
from notion_harness import (
    DEFAULT_PARENT,
    SECRET,
    build_plugins,
    call_tool,
    declared,
    live_session,
    program_probe,
    running_app,
)
from notion_stub import NotionStub
from server.app.trace import FileTraceSink

PAGE = "c" * 32

# L'instruction que toute Session où les Tools sont déclarés doit porter.
# Copie volontairement figée : elle prouve que le prompt ne dérive pas.
INSTRUCTION = (
    "Les Tools Notion ne voient que les pages partagées avec la connexion, "
    "et rien d'autre ; les bases de données et les sources de données sont "
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
        "Lit une page Notion en Markdown, par blocs, avec ses propriétés."
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

# Les six Tools de la spec #111, triés : c'est aussi l'ordre de l'instantané.
TOOL_NAMES = [
    "create_page",
    "read_page",
    "restore_page",
    "search_pages",
    "trash_page",
    "update_page",
]


def _page(page_id: str = PAGE, title: str = "Journal de bord") -> dict:
    """Objet page Notion minimal, avec sa propriété de titre."""
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id}",
        "properties": {
            "Titre": {
                "id": "title",
                "type": "title",
                "title": [
                    {
                        "type": "text",
                        "plain_text": title,
                        "text": {"content": title},
                    }
                ],
            }
        },
    }


def _markdown(content: str = "Bonjour le monde") -> dict:
    return {
        "object": "page_markdown",
        "id": PAGE,
        "markdown": content,
        "truncated": False,
        "unknown_block_ids": [],
    }


def _children() -> dict:
    return {
        "object": "list",
        "results": [{"object": "block", "id": "0" * 32, "type": "child_page"}],
        "has_more": False,
        "next_cursor": None,
    }


def _assert_french_error(payload: dict) -> None:
    """Une phrase française rendue, jamais une exception échappée."""
    assert payload["status"] == "error"
    message = payload["message"]
    assert isinstance(message, str) and message.strip()
    # ``ToolRegistry`` préfixe par « tool … » une exception qui s'échappe.
    assert not message.startswith("tool ")


# ---------------------------------------------------------------------------
# Tools et instruction dans le prompt
# ---------------------------------------------------------------------------


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
                assert notion == {
                    "name": "notion",
                    "state": "activated",
                    "tools": TOOL_NAMES,
                }

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

                async with websockets.connect(_url(transport)) as ws:
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


# ---------------------------------------------------------------------------
# Non-fuite du jeton, alors que les Traces de Tool, elles, apparaissent
# ---------------------------------------------------------------------------


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
                stub.route("GET", f"/v1/pages/{PAGE}", body=_page())
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


# ---------------------------------------------------------------------------
# 429 : exactement une requête, aucun retry, pour chacun des six Tools
# ---------------------------------------------------------------------------

# Premier appel HTTP de chaque Tool : c'est lui que le 429 doit interrompre.
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


# ---------------------------------------------------------------------------
# 503/504 sur chaque écriture : le changement peut avoir été enregistré
# ---------------------------------------------------------------------------

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
        stub.route("GET", f"/v1/pages/{PAGE}", body=_page())
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
