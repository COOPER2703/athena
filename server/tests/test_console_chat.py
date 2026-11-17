"""Tour texte de bout en bout par la Console (ticket #172, ADR-0024).

Tout passe par le vrai serveur du harnais : vraie requête HTTP, vrai flux
``text/event-stream``, vraie base mémoire, faux modèle Live scriptable. Les
scénarios observent ce qu'un navigateur voit — les événements du flux, le fil
du Journal, l'instantané — et jamais le câblage interne.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from protocol import Audio, Error, SessionStart
from server.adapters.console.contract import (
    CHAT_PATH,
    STATUS_PATH,
    THREAD_PATH,
)
from server_harness import read_sse, running_server


def test_a_text_turn_streams_tools_text_and_audio_then_returns_to_rest(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            server.model.call_tool(
                "save_memory", {"content": "j'aime le thé", "kind": "fact"}
            )
            server.model.reply("Avec plaisir.", audio=b"\x01\x02")

            async with http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "Bonjour Athena"}
            ) as response:
                assert response.status == 200
                assert response.headers["Content-Type"].startswith(
                    "text/event-stream"
                )
                assert response.headers["Cache-Control"] == "no-cache"
                assert response.headers["X-Accel-Buffering"] == "no"
                events = [event async for event in read_sse(response)]

            assert [event.event for event in events] == [
                "tool",
                "audio",
                "text",
                "done",
            ]
            tool = events[0].json()
            assert tool["name"] == "save_memory"
            assert tool["arguments"] == {"content": "j'aime le thé", "kind": "fact"}
            assert tool["status"] == "ok"
            audio = events[1].json()
            assert base64.b64decode(audio["data"]) == b"\x01\x02"
            assert audio["mime_type"] == "audio/pcm;rate=24000"
            assert events[2].json()["text"] == "Avec plaisir."
            assert events[3].json()["reason"] == "completed"

            # Les Tools du Tour sont ceux d'une Session demandée par un Client,
            # jamais la liste vide d'une Annonce.
            declared = [declaration.name for declaration in server.model.declarations()]
            assert "save_memory" in declared
            assert "end_session" in declared

            # Aucune Session n'est créée et le noyau est revenu au Repos.
            async with http.get(f"{server.url}{STATUS_PATH}") as status:
                assert status.status == 200
                snapshot = await status.json()
            assert snapshot["session"] == {
                "state": "Idle",
                "session_id": "",
                "connection_id": "",
                "active_client_id": "",
            }

    asyncio.run(scenario())


def test_the_journal_keeps_both_turns_of_a_text_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            server.model.reply("Trois choses.", audio=b"audio")

            async with http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "Compte jusqu'à trois"}
            ) as response:
                assert response.status == 200
                [event async for event in read_sse(response)]

            async with http.get(f"{server.url}{THREAD_PATH}") as response:
                assert response.status == 200
                body = await response.json()

            assert body["v"] == 1
            assert body["retention_days"] >= 1
            assert [
                (entry["role"], entry["origin"], entry["text"])
                for entry in body["entries"]
            ] == [
                ("user", "text", "Compte jusqu'à trois"),
                ("assistant", "text", "Trois choses."),
            ]
            assert all(entry["created_at"] for entry in body["entries"])
            assert body["next_before"] is None

    asyncio.run(scenario())


def test_closing_the_response_cancels_the_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            # Un modèle lent rend l'annulation observable : le Tour est encore
            # en vol quand la réponse est fermée.
            server.model.delay = 0.8
            server.model.reply("Une histoire très longue.", audio=b"audio")

            response = await http.post(
                f"{server.url}{CHAT_PATH}",
                json={"text": "Raconte une histoire"},
            )
            await server.wait_until(
                lambda: server.app.snapshot()["session"]["state"] == "LiveActive",
                timeout=2.0,
            )
            # Un Tour occupe le noyau sans jamais devenir une Session.
            during = server.app.snapshot()["session"]
            assert during["session_id"] == ""
            assert during["active_client_id"] == ""
            response.close()

            # Le noyau revient au Repos de lui-même : aucun Tour orphelin.
            await server.wait_until(
                lambda: server.app.snapshot()["session"]["state"] == "Idle",
                timeout=3.0,
            )
            await server.wait_until(
                lambda: any(
                    entry["kind"] == "TourCanceled"
                    for entry in server.trace_log()
                ),
                timeout=2.0,
            )

            # L'entrée reste au Journal (elle a été envoyée), la réponse non.
            async with http.get(f"{server.url}{THREAD_PATH}") as response:
                body = await response.json()
            assert [
                (entry["role"], entry["text"]) for entry in body["entries"]
            ] == [("user", "Raconte une histoire")]

    asyncio.run(scenario())


def test_a_turn_during_a_voice_session_is_refused_in_the_stream(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            async with server.client() as client:
                await client.start_session()
                server.model.reply("Bonjour", audio=b"reply")
                await client.send_audio(b"micro")
                assert await client.wait_message(Audio) == Audio(data=b"reply")

                async with http.post(
                    f"{server.url}{CHAT_PATH}", json={"text": "Bonjour"}
                ) as response:
                    events = [event async for event in read_sse(response)]

            assert [event.event for event in events] == ["busy"]
            refusal = events[0].json()
            assert refusal["error"] == "busy"
            assert "déjà" in refusal["message"]
            # La Session vocale n'a pas été interrompue par le refus.
            assert (
                server.app.snapshot()["session"]["state"] == "SessionVisible"
            )

    asyncio.run(scenario())


def test_a_session_requested_during_a_turn_is_refused_to_the_client(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            server.model.delay = 0.6
            server.model.reply("Une réponse.", audio=b"audio")

            response = await http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "Bonjour"}
            )
            await server.wait_until(
                lambda: server.app.snapshot()["session"]["state"] == "LiveActive",
                timeout=2.0,
            )

            async with server.client() as client:
                await client.send(SessionStart())
                refusal = await client.wait_message(Error)
                assert "Console" in refusal.message
                # Aucune Session n'a été ouverte pour le Client : le Tour ne
                # lui a envoyé ni audio, ni ``SessionStarted``.
                assert not client.queued_messages()

            response.close()
            await server.wait_until(
                lambda: server.app.snapshot()["session"]["state"] == "Idle",
                timeout=3.0,
            )

    asyncio.run(scenario())


def test_a_turn_during_another_turn_is_refused_and_the_first_still_answers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            server.model.delay = 0.4
            server.model.reply("Une seule réponse.", audio=b"audio")

            first = await http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "Premier"}
            )
            await server.wait_until(
                lambda: server.app.snapshot()["session"]["state"] != "Idle",
                timeout=2.0,
            )
            async with http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "Deuxième"}
            ) as second:
                refused = [event async for event in read_sse(second)]
            assert [event.event for event in refused] == ["busy"]

            answer = [event async for event in read_sse(first)]
            assert [event.event for event in answer] == ["audio", "text", "done"]
            assert answer[-1].json()["reason"] == "completed"

    asyncio.run(scenario())


def test_the_thread_is_paginated_by_a_stable_cursor(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            for index in range(3):
                server.model.reply(f"Réponse {index}", audio=b"audio")
                async with http.post(
                    f"{server.url}{CHAT_PATH}",
                    json={"text": f"Question {index}"},
                ) as response:
                    [event async for event in read_sse(response)]

            async with http.get(
                f"{server.url}{THREAD_PATH}?limit=2"
            ) as response:
                assert response.status == 200
                first = await response.json()
            assert [entry["text"] for entry in first["entries"]] == [
                "Question 2",
                "Réponse 2",
            ]
            cursor = first["next_before"]
            assert cursor == first["entries"][0]["id"]

            async with http.get(
                f"{server.url}{THREAD_PATH}?limit=2&before={cursor}"
            ) as response:
                second = await response.json()
            assert [entry["text"] for entry in second["entries"]] == [
                "Question 1",
                "Réponse 1",
            ]

            async with http.get(
                f"{server.url}{THREAD_PATH}?limit=10&before=1"
            ) as response:
                empty = await response.json()
            assert empty["entries"] == []
            assert empty["next_before"] is None

            async with http.get(f"{server.url}{THREAD_PATH}?limit=0") as response:
                assert response.status == 400
                refusal = await response.json()
            assert refusal["error"] == "invalid_limit"
            assert "limit" in refusal["fields"]

    asyncio.run(scenario())


def test_end_session_cannot_close_a_text_turn(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            # Le modèle demande la clôture : hors Session ordinaire, le noyau
            # refuse et le Tour continue jusqu'à sa réponse.
            server.model.call_tool("end_session")
            server.model.reply("Je reste là.", audio=b"audio")

            async with http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "Termine"}
            ) as response:
                events = [event async for event in read_sse(response)]

            assert [event.event for event in events] == [
                "tool",
                "audio",
                "text",
                "done",
            ]
            assert events[0].json()["status"] == "error"
            assert events[-1].json()["reason"] == "completed"

    asyncio.run(scenario())


def test_a_turn_without_answer_ends_on_its_guard_timer(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            # Un modèle qui ne rend rien : seul le garde-fou referme le Tour,
            # et la surface reçoit une fin au lieu d'un flux ouvert.
            server.model.delay = 5.0
            server.model.reply("Trop tard.", audio=b"audio")

            response = await http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "Bonjour"}
            )
            await server.wait_until(
                lambda: any(
                    connection_id.startswith("t")
                    for connection_id in server.clock.scheduled
                ),
                timeout=2.0,
            )
            server.clock.advance(30)

            events = [event async for event in read_sse(response)]
            assert [event.event for event in events] == ["done"]
            assert events[0].json()["reason"] == "timeout"
            assert server.app.snapshot()["session"]["state"] == "Idle"

    asyncio.run(scenario())


def test_a_model_failure_is_an_error_event_and_frees_the_kernel(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            server.model.fail()

            async with http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "Bonjour"}
            ) as response:
                events = [event async for event in read_sse(response)]

            assert [event.event for event in events] == ["error"]
            failure = events[0].json()
            assert failure["error"] == "llm_failed"
            assert failure["message"]
            await server.wait_until(
                lambda: server.app.snapshot()["session"]["state"] == "Idle",
                timeout=2.0,
            )

    asyncio.run(scenario())


def test_the_thread_marks_voice_turns_and_they_feed_the_next_turn(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            server.model.reply("Trois.", audio=b"audio")

            # Un Tour texte, puis une Session vocale : un seul Journal, un seul
            # cerveau. L'origine distingue les deux, l'historique les réunit.
            async with http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "Compte jusqu'à trois"}
            ) as response:
                [event async for event in read_sse(response)]

            async with server.client() as client:
                await client.start_session()
                server.model.reply("Deux.", audio=b"reply", heard="Encore")
                await client.send_audio(b"micro")
                assert await client.wait_message(Audio) == Audio(data=b"reply")

            async with http.get(f"{server.url}{THREAD_PATH}") as response:
                body = await response.json()
            assert [
                (entry["role"], entry["origin"], entry["text"])
                for entry in body["entries"]
            ] == [
                ("user", "text", "Compte jusqu'à trois"),
                ("assistant", "text", "Trois."),
                ("user", "voice", "Encore"),
                ("assistant", "voice", "Deux."),
            ]

            # Le contexte de la Session vocale porte les tours du Tour texte :
            # l'origine ne change rien à l'historique conversationnel.
            instruction = server.model.instruction()
            assert "user: Compte jusqu'à trois" in instruction
            assert "assistant: Trois." in instruction

    asyncio.run(scenario())


def test_an_empty_message_is_refused_with_a_field_error(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            async with http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "   "}
            ) as response:
                assert response.status == 400
                body = await response.json()
            assert body["error"] == "invalid_message"
            assert body["fields"]["text"]
            assert server.model.sessions == []

    asyncio.run(scenario())
