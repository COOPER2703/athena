"""Tests du ticket #118 : ``trash_page`` et ``restore_page``.

Le faux serveur Notion et le harnais de #112 sont réutilisés tels quels : chaque
test observe les requêtes HTTP émises, le payload rendu au modèle et les Traces.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conftest import RecordingTraceSink
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

PAGE_ID = "e" * 32


def _page(
    page_id: str = PAGE_ID,
    title: str = "Journal de bord",
    *,
    url: str | None = None,
) -> dict:
    """Objet page Notion minimal, avec sa propriété de titre."""
    return {
        "object": "page",
        "id": page_id,
        "url": url if url is not None else f"https://www.notion.so/{page_id}",
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


def _writes(stub: NotionStub) -> list[dict]:
    """Toute requête qui n'est pas un GET : un refus ne doit rien écrire."""
    return [request for request in stub.requests if request["method"] != "GET"]


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

                assert set(tools) == {
                    "search_pages",
                    "trash_page",
                    "restore_page",
                }
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
            stub.route("GET", _page_path(), body=_page())
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
            stub.route("PATCH", _page_path(), body=_page())
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
            stub.route("GET", _page_path(), body=_page())
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
            stub.route("GET", _page_path(), body=_page())
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
                assert "peut-être enregistré" in payload["message"]
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
            stub.route("PATCH", _page_path(), body=_page())
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
            stub.route("GET", _page_path(), body=_page(title="Ressuscitée"))
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
                assert "peut-être enregistré" in payload["message"]
                assert len(stub.requests_for("PATCH", _page_path())) == 1
                trace = sink.of_kind("RestorePage")[-1].payload
                assert trace["reason"] == f"HTTP {status}"
                assert trace["status"] == "error"

    asyncio.run(scenario())
