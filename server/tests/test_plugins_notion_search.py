from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import websockets

from conftest import FakeJudge, RecordingTraceSink, _url, register, wait_until
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


# ---------------------------------------------------------------------------
# Sonde de démarrage
# ---------------------------------------------------------------------------


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
                # La Console montre le plugin activé ; les tickets suivants
                # ajoutent leurs Tools à cette même ligne.
                [row] = app.snapshot()["plugins"]
                assert row["name"] == "notion"
                assert row["state"] == "activated"
                assert "search_pages" in row["tools"]

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


# ---------------------------------------------------------------------------
# Tool ``search_pages`` : App → Gemini → registre → plugin → faux Notion.
# ---------------------------------------------------------------------------

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


def _page(
    page_id: str,
    title: str,
    *,
    url: str | None = None,
    edited: str = "2026-10-01T10:00:00.000Z",
) -> dict:
    """Objet page Notion minimal, avec sa propriété de titre."""
    return {
        "object": "page",
        "id": page_id,
        "url": url if url is not None else f"https://www.notion.so/{page_id}",
        "last_edited_time": edited,
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
                # Le Tool est visible dans l'instantané de la Console ; les
                # tickets suivants ajoutent leurs Tools à cette même ligne.
                [row] = app.snapshot()["plugins"]
                assert row["name"] == "notion"
                assert row["state"] == "activated"
                assert "search_pages" in row["tools"]

                async with live_session(transport, live) as session:
                    tools = declared(live)
                    assert "search_pages" in tools
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


# ---------------------------------------------------------------------------
# Recette : le secret ne fuit pas, l'Annonce ne déclare aucun Tool.
# ---------------------------------------------------------------------------


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
                    # La fenêtre de regroupement dure 5 s (ADR-0007).
                    await wait_until(
                        lambda: bool(app_sink.of_kind("EventJudge")),
                        timeout=7.0,
                    )

                # La Connexion Live d'Annonce est ouverte sans aucun outil.
                assert live.declarations() == []
                assert app.plugins.states["notion"] == "activated"

    asyncio.run(scenario())
