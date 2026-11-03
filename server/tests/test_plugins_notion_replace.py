"""Tool ``update_page`` : remplacement protégé du corps (ticket #117).

Les tests observent la couture HTTP : le faux serveur enregistre chaque
requête, le payload rendu au modèle et les Traces. Un remplacement n'est
jamais écrit sans ``confirm: true``, ne supprime jamais une sous-page et
n'est jamais rejoué.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import RecordingTraceSink
from notion_harness import (
    SECRET,
    build_plugins,
    call_tool,
    live_session,
    program_probe,
    running_app,
)
from notion_stub import NotionStub

TARGET = "f" * 32

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


def _page(
    page_id: str,
    title: str,
    *,
    key: str = "Titre",
    url: str | None = None,
) -> dict:
    """Objet page Notion minimal, avec sa propriété de titre nommable."""
    return {
        "object": "page",
        "id": page_id,
        "url": url if url is not None else f"https://www.notion.so/{page_id}",
        "last_edited_time": "2026-10-01T10:00:00.000Z",
        "properties": {
            key: {
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


def _summary(page: dict) -> dict:
    """La forme ``page`` rendue au modèle : id, titre, URL."""
    return {"id": page["id"], "title": _title(page), "url": page["url"]}


def _title(page: dict) -> str:
    for prop in page["properties"].values():
        if prop.get("type") == "title":
            return "".join(
                part.get("plain_text", "") for part in prop.get("title", [])
            )
    return "(sans titre)"


def _writes(stub: NotionStub) -> list[dict]:
    return [request for request in stub.requests if request["method"] != "GET"]


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
                assert trace.payload["warnings"] == 1
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
                assert replaced.payload["warnings"] == 0
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
