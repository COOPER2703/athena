"""Tableaux et colonnes à travers le Markdown Notion (ticket #119).

Chaque test passe par la couture réelle : App → faux Gemini Live → registre →
plugin copié → faux serveur Notion. Le plugin écrit le Markdown verbatim et
rend celui que Notion a rendu : aucun helper privé n'est touché.

Le Markdown ``v2`` sépare deux blocs de premier niveau par un seul saut de
ligne ; les fixtures suivent cette forme, et non le double saut de ligne des
Markdown classiques.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, AsyncIterator

from conftest import RecordingTraceSink
from notion_harness import (
    build_plugins,
    call_tool,
    live_session,
    program_probe,
    running_app,
)
from notion_stub import NotionStub

PAGE = "9a" * 16

PIPE_TABLE = (
    "| Nom | Rôle |\n"
    "| --- | --- |\n"
    "| Ada | Poète |"
)

# Tableau stylé : une ligne vide peut séparer les rangées et les cellules
# multilignes ; le bloc reste entier malgré ces lignes vides.
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

# Un groupe imbriqué dans une colonne : la balise interne ne doit pas clore
# le groupe externe trop tôt.
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

# Balises lues que le plugin doit transmettre telles quelles.
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
    "- un\n- deux",
]
V2_PAGE = "\n".join(V2_BLOCKS)
TABLE_INDEX = V2_BLOCKS.index(PIPE_TABLE)
COLUMNS_INDEX = V2_BLOCKS.index(COLUMNS)
NESTED_INDEX = V2_BLOCKS.index(NESTED_COLUMNS)


def _page(page_id: str, title: str) -> dict:
    """Objet page Notion minimal, avec sa propriété de titre."""
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id}",
        "last_edited_time": "2026-10-01T10:00:00.000Z",
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


def _markdown_body(markdown: str) -> dict:
    """Réponse de ``GET /v1/pages/{id}/markdown``."""
    return {
        "object": "page_markdown",
        "id": PAGE,
        "markdown": markdown,
        "truncated": False,
        "unknown_block_ids": [],
    }


@contextlib.asynccontextmanager
async def _notion_session(
    tmp_path: Path, stub: NotionStub
) -> AsyncIterator[tuple[Any, RecordingTraceSink]]:
    """App réelle, Connexion Live, plugin réel branché sur le faux serveur."""
    sink = RecordingTraceSink()
    registry, loader = build_plugins(tmp_path, sink, stub.base_url)
    async with running_app(registry=registry, loader=loader) as (
        _,
        live,
        transport,
        _,
    ):
        async with live_session(transport, live) as session:
            yield session, sink


def _writes(stub: NotionStub) -> list[dict]:
    """Les requêtes d'écriture reçues par le faux serveur."""
    return [request for request in stub.requests if request["method"] != "GET"]


def _table_only(stub: NotionStub, markdown: str) -> None:
    """Programme une page dont le corps est exactement ``markdown``."""
    program_probe(stub)
    stub.route("GET", f"/v1/pages/{PAGE}", body=_page(PAGE, "Atelier"))
    stub.route(
        "GET",
        f"/v1/pages/{PAGE}/markdown",
        body=_markdown_body(markdown),
    )


def test_create_page_sends_a_pipe_table_verbatim_with_markdown_v2(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            program_probe(stub)
            stub.route(
                "POST", "/v1/pages", body=_page(PAGE, "Notes")
            )
            async with _notion_session(tmp_path, stub) as (session, _):
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
                "POST", "/v1/pages", body=_page(PAGE, "Colonnes")
            )
            async with _notion_session(tmp_path, stub) as (session, _):
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
                "GET", f"/v1/pages/{PAGE}", body=_page(PAGE, "Atelier")
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{PAGE}/markdown",
                body=_markdown_body(PIPE_TABLE),
            )
            async with _notion_session(tmp_path, stub) as (session, _):
                payload = await call_tool(
                    session,
                    "update_page",
                    {"page": PAGE, "content": PIPE_TABLE},
                )

            assert payload["status"] == "ok"
            assert payload["content_updated"] is True
            [request] = stub.requests_for(
                "PATCH", f"/v1/pages/{PAGE}/markdown"
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
                "GET", f"/v1/pages/{PAGE}", body=_page(PAGE, "Atelier")
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{PAGE}/markdown",
                body=_markdown_body(COLUMNS),
            )
            async with _notion_session(tmp_path, stub) as (session, _):
                payload = await call_tool(
                    session,
                    "update_page",
                    {"page": PAGE, "content": COLUMNS},
                )

            assert payload["status"] == "ok"
            [request] = stub.requests_for(
                "PATCH", f"/v1/pages/{PAGE}/markdown"
            )
            assert request["body"]["insert_content"]["content"] == COLUMNS
            assert request["body"]["markdown_version"] == "v2"
            assert len(_writes(stub)) == 1

    asyncio.run(scenario())


def test_read_page_returns_a_pipe_table_verbatim(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            _table_only(stub, PIPE_TABLE)
            async with _notion_session(tmp_path, stub) as (session, _):
                payload = await call_tool(
                    session, "read_page", {"page": PAGE}
                )

            assert payload["status"] == "ok"
            # Notion a rendu une pipe table : elle ressort identique.
            assert payload["content"] == PIPE_TABLE
            assert payload["total_blocks"] == 1
            [request] = stub.requests_for(
                "GET", f"/v1/pages/{PAGE}/markdown"
            )
            assert request["query"] == {"markdown_version": ["v2"]}

    asyncio.run(scenario())


def test_read_page_returns_a_styled_table_verbatim(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with NotionStub() as stub:
            _table_only(stub, STYLED_TABLE)
            async with _notion_session(tmp_path, stub) as (session, _):
                payload = await call_tool(
                    session, "read_page", {"page": PAGE}
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
            async with _notion_session(tmp_path, stub) as (session, _):
                payload = await call_tool(
                    session, "read_page", {"page": PAGE}
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
            async with _notion_session(tmp_path, stub) as (session, _):
                chunks = []
                offsets = []
                offset = 0
                while True:
                    payload = await call_tool(
                        session,
                        "read_page",
                        {"page": PAGE, "offset": offset, "limit": 1},
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
            async with _notion_session(tmp_path, stub) as (session, _):
                around_table = await call_tool(
                    session,
                    "read_page",
                    {"page": PAGE, "offset": TABLE_INDEX - 1, "limit": 2},
                )
                around_columns = await call_tool(
                    session,
                    "read_page",
                    {"page": PAGE, "offset": COLUMNS_INDEX, "limit": 3},
                )
                table_alone = await call_tool(
                    session,
                    "read_page",
                    {"page": PAGE, "offset": TABLE_INDEX, "limit": 1},
                )
                around_nested = await call_tool(
                    session,
                    "read_page",
                    {"page": PAGE, "offset": NESTED_INDEX - 1, "limit": 2},
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
                "POST", "/v1/pages", body=_page(PAGE, "Tableau")
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{PAGE}/markdown",
                body=_markdown_body(PIPE_TABLE),
            )
            async with _notion_session(tmp_path, stub) as (session, _):
                read = await call_tool(
                    session, "read_page", {"page": PAGE}
                )
                created = await call_tool(
                    session,
                    "create_page",
                    {"title": "Tableau", "content": PIPE_TABLE},
                )
                appended = await call_tool(
                    session,
                    "update_page",
                    {"page": PAGE, "content": PIPE_TABLE},
                )

        assert read["status"] == "ok"
        assert created["status"] == "ok"
        assert appended["status"] == "ok"

        [read_request] = stub.requests_for(
            "GET", f"/v1/pages/{PAGE}/markdown"
        )
        [create_request] = stub.requests_for("POST", "/v1/pages")
        [append_request] = stub.requests_for(
            "PATCH", f"/v1/pages/{PAGE}/markdown"
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
            async with _notion_session(tmp_path, stub) as (session, sink):
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
                "GET", f"/v1/pages/{PAGE}", body=_page(PAGE, "Atelier")
            )
            stub.route(
                "PATCH",
                f"/v1/pages/{PAGE}/markdown",
                status=400,
                body={
                    "object": "error",
                    "status": 400,
                    "code": "validation_error",
                    "message": "The request body was invalid.",
                },
            )
            async with _notion_session(tmp_path, stub) as (session, sink):
                payload = await call_tool(
                    session,
                    "update_page",
                    {"page": PAGE, "content": invalid},
                )

                assert payload["status"] == "error"
                assert "refusé" in payload["message"]
                assert "contenu invalide" in payload["message"]
                # Exactement une écriture, jamais rejouée, rien d'autre.
                assert (
                    len(
                        stub.requests_for(
                            "PATCH", f"/v1/pages/{PAGE}/markdown"
                        )
                    )
                    == 1
                )
                assert stub.requests_for(
                    "PATCH", f"/v1/pages/{PAGE}"
                ) == []
                assert len(_writes(stub)) == 1
                [update] = sink.of_kind("UpdatePage")
                assert update.payload["status"] == "error"
                assert update.payload["reason"] == "HTTP 400"

    asyncio.run(scenario())
