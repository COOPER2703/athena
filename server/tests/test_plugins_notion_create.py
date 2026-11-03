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


def _created_page(
    page_id: str,
    title: str,
    *,
    url: str | None = None,
) -> dict:
    """Objet page rendu par ``POST /v1/pages``, avec sa propriété de titre."""
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
                # L'ensemble exact des six Tools est asserté par le test
                # transverse du ticket #120 : ici, on tolère les Tools des
                # tickets suivants.
                snapshot = app.snapshot()["plugins"]
                assert len(snapshot) == 1
                [notion] = snapshot
                assert notion["name"] == "notion"
                assert notion["state"] == "activated"
                assert {"create_page", "search_pages"} <= set(
                    notion["tools"]
                )

                async with live_session(transport, live):
                    tools = declared(live)
                    assert {"create_page", "search_pages"} <= set(tools)
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
                "POST", "/v1/pages", body=_created_page(page_id, "Idée")
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
                body=_created_page("c" * 32, "Note"),
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
                body=_created_page(
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
                "POST", "/v1/pages", body=_created_page(page_id, "Plan")
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
                body=_created_page("8" * 32, "Trop tard"),
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
