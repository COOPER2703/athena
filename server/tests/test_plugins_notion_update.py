"""Tool ``update_page`` : ajout et renommage d'une page Notion (ticket #116).

Les tests observent la couture HTTP : le faux serveur enregistre chaque
requête, le payload rendu au modèle, et les Traces. Le remplacement du corps
appartient au ticket #117 : ici, ``mode=replace`` est refusé sans écriture.
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
    declared,
    live_session,
    program_probe,
    running_app,
)
from notion_stub import NotionStub

TARGET = "f" * 32

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


def _append_body(content: str) -> dict:
    return {
        "type": "insert_content",
        "insert_content": {
            "content": content,
            "position": {"type": "end"},
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

                assert set(tools) == {"search_pages", "update_page"}
                spec = tools["update_page"]
                assert dict(spec.parameters_json_schema) == UPDATE_PARAMETERS
                # Le mode est optionnel et vaut append : la déclaration ne
                # l'exige pas, la description l'annonce.
                assert "append" in spec.parameters_json_schema["properties"][
                    "mode"
                ]["description"]
                assert "ajoute" in spec.description

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
                assert trace.payload["warnings"] == 1

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
                    with_confirm = await call_tool(
                        session,
                        "update_page",
                        {
                            "page": TARGET,
                            "content": "Tout",
                            "mode": "replace",
                            "confirm": True,
                        },
                    )

                for payload in (without, with_confirm):
                    assert payload["status"] == "error"
                    assert "remplacement" in payload["message"]
                    assert "confirmation" in payload["message"]
                # Aucune écriture : #117 possède le remplacement.
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
                assert trace.payload["warnings"] == 0
                assert trace.payload["status"] == "ok"
                assert isinstance(trace.payload["duration_ms"], (int, float))
                traces = json.dumps(
                    [entry.payload for entry in (*sink.entries, *app_sink.entries)],
                    default=str,
                )
                assert content not in traces
                assert SECRET not in traces

    asyncio.run(scenario())
