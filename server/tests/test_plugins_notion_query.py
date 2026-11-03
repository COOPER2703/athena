"""Filtre par mot de ``read_page`` (ticket #114).

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
        "query": {
            "type": "string",
            "description": "Mot à chercher dans les paragraphes de cette page.",
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


def _markdown_body(markdown: str) -> dict:
    """Réponse de ``GET /v1/pages/{id}/markdown``."""
    return {
        "object": "page_markdown",
        "id": "page",
        "markdown": markdown,
        "truncated": False,
        "unknown_block_ids": [],
    }


@contextlib.asynccontextmanager
async def _notion_session(
    tmp_path: Path,
    stub: NotionStub,
    *,
    sink: RecordingTraceSink | None = None,
) -> AsyncIterator[
    tuple[Any, Any, Any, RecordingTraceSink, RecordingTraceSink]
]:
    """App réelle, Connexion Live, plugin réel branché sur le faux serveur."""
    sink = sink if sink is not None else RecordingTraceSink()
    registry, loader = build_plugins(tmp_path, sink, stub.base_url)
    async with running_app(registry=registry, loader=loader) as (
        app,
        live,
        transport,
        app_sink,
    ):
        async with live_session(transport, live) as session:
            yield app, live, session, sink, app_sink


async def _served_page(
    stub: NotionStub, page: str, markdown: str, title: str = "Atelier"
) -> None:
    """Programme la page et son Markdown sur le faux serveur."""
    stub.route("GET", f"/v1/pages/{page}", body=_page(page, title))
    stub.route(
        "GET", f"/v1/pages/{page}/markdown", body=_markdown_body(markdown)
    )


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
