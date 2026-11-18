"""Le harnais de test serveur se prouve lui-même.

Ces tests parlent au vrai serveur démarré par ``running_server`` : vraies
requêtes HTTP, vrai transport WebSocket, vrai modèle Live remplacé par un
adaptateur de test. Ils ne montent rien à la main.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness import wait_until
from protocol import Audio, SessionEndReason, SessionEnded
from server.adapters.console.contract import HEALTH_PATH, SESSION_PATH, STATUS_PATH
from server_harness import FakeEmbedder, ManualClock, read_sse, running_server


def test_a_real_http_request_reaches_healthz_and_session(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            async with server.anonymous() as http:
                async with http.get(f"{server.url}{HEALTH_PATH}") as response:
                    assert response.status == 200
                    assert await response.json() == {"status": "ok"}

                async with http.get(f"{server.url}{SESSION_PATH}") as response:
                    assert response.status == 200
                    identity = await response.json()
                    assert identity["v"] == 1
                    assert identity["setup_required"] is True
                    assert identity["authenticated"] is False

    asyncio.run(scenario())


def test_install_opens_a_ticket_and_unlocks_the_api(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()

            async with http.get(f"{server.url}{STATUS_PATH}") as response:
                assert response.status == 200
                snapshot = await response.json()
            assert snapshot["session"]["state"] == "Idle"

            # Un autre navigateur, sans Ticket, ne voit toujours rien.
            async with server.anonymous() as anonymous:
                async with anonymous.get(f"{server.url}{STATUS_PATH}") as response:
                    assert response.status == 401

            # L'identité reflète l'installation, sans redémarrage.
            async with http.get(f"{server.url}{SESSION_PATH}") as response:
                identity = await response.json()
            assert identity["setup_required"] is False
            assert identity["authenticated"] is True

    asyncio.run(scenario())


def test_each_server_gets_its_own_directories_and_account(tmp_path: Path) -> None:
    async def scenario() -> None:
        first_root = tmp_path / "first"
        second_root = tmp_path / "second"

        async with running_server(first_root) as first:
            await first.install()
            assert first.paths.auth_db.is_file()
            assert first.paths.memory_db.is_file()
            assert first.paths.log_file.is_file()

        async with running_server(second_root) as second:
            # Base neuve : l'instance est encore à installer.
            async with second.anonymous() as http:
                async with http.get(f"{second.url}{SESSION_PATH}") as response:
                    assert (await response.json())["setup_required"] is True
            assert second.paths.root != first.paths.root

    asyncio.run(scenario())


def test_the_fake_model_replies_with_text_and_audio(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            server.model.reply("Bonjour", audio=b"voix")

            async with server.client() as client:
                await client.start_session()
                await client.send_audio(b"micro")

                assert await client.wait_message(Audio) == Audio(data=b"voix")

            await wait_until(lambda: server.model.received_audio == [b"micro"])
            await wait_until(
                lambda: server.query(
                    server.paths.memory_db,
                    "SELECT role, content FROM journal",
                )
                == [{"role": "assistant", "content": "Bonjour"}]
            )

    asyncio.run(scenario())


def test_the_fake_model_calls_a_kernel_tool_and_receives_its_result(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            server.model.call_tool(
                "save_memory",
                {"content": "aime le bleu", "kind": "profile"},
            )
            server.model.reply("C'est noté.", audio=b"ok")

            async with server.client() as client:
                await client.start_session()
                await client.send_audio(b"retiens que j'aime le bleu")
                assert await client.wait_message(Audio) == Audio(data=b"ok")

            await wait_until(lambda: bool(server.model.tool_results))
            assert server.model.tool_results[0]["name"] == "save_memory"
            assert server.model.tool_results[0]["response"]["status"] == "ok"

            facts = server.query(server.paths.memory_db, "SELECT content FROM facts")
            assert [fact["content"] for fact in facts] == ["aime le bleu"]

    asyncio.run(scenario())


def test_the_model_can_be_slowed_so_a_turn_stays_in_flight(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            server.model.delay = 0.3
            server.model.reply("Lentement", audio=b"tard")

            async with server.client() as client:
                await client.start_session()
                await client.send_audio(b"micro")

                await asyncio.sleep(0.05)
                assert not client.queued_messages()

                assert await client.wait_message(Audio, timeout=2.0) == Audio(
                    data=b"tard"
                )

    asyncio.run(scenario())


def test_the_manual_clock_advances_the_idle_timeout(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            server.model.reply("Me voilà", audio=b"voix")

            async with server.client() as client:
                await client.start_session()
                await client.send_audio(b"micro")
                await client.wait_message(Audio)

                # La Session est visible : son inactivité est de nouveau armée.
                await wait_until(lambda: server.clock.scheduled)
                server.clock.advance(server.clock.next_timeout())

                ended = await client.wait_message(SessionEnded)
                assert ended.reason is SessionEndReason.TIMEOUT

    asyncio.run(scenario())


def test_the_manual_clock_also_expires_the_console_ticket(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()

            async with http.get(f"{server.url}{STATUS_PATH}") as response:
                assert response.status == 200

            server.clock.advance(31 * 24 * 60 * 60)

            async with http.get(f"{server.url}{STATUS_PATH}") as response:
                assert response.status == 401

    asyncio.run(scenario())


def test_the_console_stream_replays_the_backlog_then_feeds_live_traces(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()

            async with server.trace_stream() as stream:
                assert stream.hello["type"] == "hello"
                assert "started" in [trace["kind"] for trace in stream.backlog]

                server.emit_log("fixture", "direct")
                trace = await stream.next_trace("fixture")
                assert trace["source"] == "logging"

    asyncio.run(scenario())


def test_the_console_serves_a_configured_static_build(tmp_path: Path) -> None:
    async def scenario() -> None:
        build = tmp_path / "dist"
        build.mkdir()
        (build / "index.html").write_text(
            '<!doctype html><div id="root"></div>', encoding="utf-8"
        )

        async with running_server(tmp_path, static_dir=build) as server:
            async with server.anonymous() as http:
                async with http.get(f"{server.url}/") as response:
                    assert response.status == 200
                    assert 'id="root"' in await response.text()

    asyncio.run(scenario())


def test_the_fake_model_receives_text_and_speaks_it_back(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            # Le Tool épingle un fait, puis la prise de parole suivante le
            # rappelle : le noyau injecte le bloc [MÉMOIRE] comme texte.
            server.model.call_tool(
                "save_memory", {"content": "bonjour", "kind": "profile"}
            )
            server.model.reply(heard="bonjour", text="Je m'en souviens.")

            async with server.client() as client:
                await client.start_session()
                await client.send_audio(b"bonjour")

                await wait_until(
                    lambda: any(
                        "[MÉMOIRE]" in text
                        for text in server.model.received_texts
                    )
                )
            assert any(
                "bonjour" in text for text in server.model.received_texts
            )

    asyncio.run(scenario())


def test_the_sse_reader_parses_a_tour_stream() -> None:
    class _Lines:
        def __init__(self, lines: list[bytes]) -> None:
            self._lines = lines

        async def __aiter__(self):
            for line in self._lines:
                yield line

    class _Response:
        content = _Lines(
            [
                b"event: text\n",
                b'data: {"delta": "Bon"}\n',
                b"\n",
                b": commentaire ignore\n",
                b"event: tool\n",
                b'data: {"name": "save_memory"}\n',
                b"data: ligne 2\n",
                b"\n",
                b"event: done\n",
                b'data: {"reason": "normal"}',
            ]
        )

    async def scenario() -> None:
        events = [event async for event in read_sse(_Response())]
        assert [(event.event, event.data) for event in events] == [
            ("text", '{"delta": "Bon"}'),
            ("tool", '{"name": "save_memory"}\nligne 2'),
            ("done", '{"reason": "normal"}'),
        ]
        assert events[0].json() == {"delta": "Bon"}

    asyncio.run(scenario())


def test_manual_clock_and_embedder_are_deterministic() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        clock.schedule("a", 5.0)
        clock.schedule("b", 1.0)
        assert clock.next_timeout() == 1.0

        clock.advance(1.0)
        fired = await clock.recv()
        assert fired.connection_id == "b"
        assert clock.pending == {"a": 4.0}

        embedder = FakeEmbedder()
        vector = await embedder.embed("bonjour")
        assert len(vector) == 384
        assert vector == await embedder.embed("bonjour")
        assert vector != await embedder.embed("bonsoir")

    asyncio.run(scenario())
