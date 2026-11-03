"""Lecture Markdown paginée d'une page Notion (ticket #113).

Chaque test passe par la couture réelle : App → faux Gemini Live → registre →
plugin copié → faux serveur Notion. Aucun helper privé n'est touché.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, AsyncIterator

from conftest import RecordingTraceSink
from notion_harness import (
    SECRET,
    build_plugins,
    call_tool,
    declared,
    live_session,
    program_probe,
    running_app,
)
from notion_stub import NotionStub

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
    },
    "required": ["page"],
}


def _page(
    page_id: str,
    title: str,
    *,
    properties: dict[str, Any] | None = None,
    edited: str = "2026-10-01T10:00:00.000Z",
) -> dict:
    """Objet page Notion minimal, avec sa propriété de titre."""
    rendered = {
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
    }
    rendered.update(properties or {})
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://www.notion.so/{page_id}",
        "last_edited_time": edited,
        "properties": rendered,
    }


def _markdown_body(
    markdown: str,
    *,
    truncated: bool = False,
    unknown_block_ids: list[str] | None = None,
) -> dict:
    """Réponse de ``GET /v1/pages/{id}/markdown``."""
    return {
        "object": "page_markdown",
        "id": "page",
        "markdown": markdown,
        "truncated": truncated,
        "unknown_block_ids": unknown_block_ids or [],
    }


@contextlib.asynccontextmanager
async def _notion_session(
    tmp_path: Path,
    stub: NotionStub,
    *,
    sink: RecordingTraceSink | None = None,
    timeout: float | None = None,
) -> AsyncIterator[
    tuple[Any, Any, Any, RecordingTraceSink, RecordingTraceSink]
]:
    """App réelle, Connexion Live, plugin réel branché sur le faux serveur."""
    sink = sink if sink is not None else RecordingTraceSink()
    registry, loader = build_plugins(
        tmp_path, sink, stub.base_url, timeout=timeout
    )
    async with running_app(registry=registry, loader=loader) as (
        app,
        live,
        transport,
        app_sink,
    ):
        async with live_session(transport, live) as session:
            yield app, live, session, sink, app_sink


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
                # L'ensemble exact des six Tools est asserté par le test
                # transverse du ticket #120 : ici, on tolère les Tools des
                # tickets suivants.
                assert {"read_page", "search_pages"} <= set(plugin["tools"])
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


# Markdown de référence : chaque construction à ne jamais couper.
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
    "- un\n- deux",
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
        assert first["total_blocks"] == 7

        assert second["content"] == "\n\n".join(EXPECTED_BLOCKS[3:6])
        assert second["offset"] == first["next_offset"]
        assert second["next_offset"] == 6
        assert second["has_more"] is True

        assert third["content"] == EXPECTED_BLOCKS[6]
        assert third["offset"] == second["next_offset"]
        assert third["next_offset"] == 7
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
                        "Vide": {"type": "rich_text", "rich_text": []},
                        "Sans date": {"type": "date", "date": None},
                        "Identifiant": {
                            "type": "unique_id",
                            "unique_id": {"prefix": "A", "number": 4},
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
            assert trace.payload["total_blocks"] == 7
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
