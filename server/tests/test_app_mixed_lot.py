"""Lot mixte de bout en bout : une seule Annonce, deux faits (#198).

Couture de composition : la vraie racine (``build_app`` via ``running_server``),
le vrai transport, la mémoire SQLite et le faux modèle Live. Un Événement
ordinaire et une Occurrence du Planificateur tombent dans la même fenêtre ; on
observe qu'**une seule** Connexion d'Annonce s'ouvre, que le faux modèle reçoit
la phrase du juge puis la consigne de rappel, et que les deux faits sont des
faits — jamais la phrase prononcée. Le Journal reçoit les deux une fois, en
rôle ``event``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from testkit import FakeJudge
from protocol import Audio
from server.core import (
    EVENT_WINDOW_TIMEOUT,
    OCCURRENCE_TRIGGER,
    OccurrenceReceived,
)
from server_harness import running_server


def test_a_mixed_lot_produces_a_single_announcement_with_both_facts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        judge = FakeJudge(message="La porte est ouverte")
        async with running_server(tmp_path, judge=judge) as server:
            await server.install()
            async with server.trace_stream() as stream:
                async with server.client() as client:
                    # Le modèle répond au tour d'Annonce une fois qu'il est
                    # ouvert ; l'audio rend la Session visible et journalise.
                    server.model.reply(text="La porte est ouverte", audio=b"rappel")

                    # L'Événement ordinaire arme la fenêtre, l'Occurrence la
                    # rejoint : on attend que les deux soient en File avant
                    # l'échéance, pour ne pas dépendre d'un ordre d'exécution.
                    server.app.push_event("matrix", "porte ouverte")
                    await stream.next_trace("EventQueued")
                    server.app.push_occurrence(
                        OccurrenceReceived(
                            content="sortir les poubelles", schedule_id="sch_1"
                        )
                    )
                    await stream.next_trace("OccurrenceQueued")

                    server.clock.advance(EVENT_WINDOW_TIMEOUT)

                    assert await client.wait_message(Audio, timeout=2.0) == Audio(
                        data=b"rappel"
                    )

                    # Une seule Connexion Live : une seule Annonce.
                    assert len(server.model.configs) == 1
                    # Le juge n'a vu que l'Événement ordinaire.
                    assert judge.calls == [["[matrix] porte ouverte"]]

                    # L'adaptateur envoie d'abord les faits, puis la phrase.
                    prompts = server.model.received_texts
                    text = prompts[-1]
                    facts = "\n".join(prompts[:-1])
                    assert text == (
                        f"La porte est ouverte\n\n{OCCURRENCE_TRIGGER}"
                    )
                    assert "poubelles" not in text
                    assert "[matrix] porte ouverte" in facts
                    assert "[planificateur] sortir les poubelles" in facts

        # Le Journal reçoit les deux faits annoncés, une fois, en rôle ``event``.
        journal = server.query(
            server.paths.memory_db,
            "SELECT role, content FROM journal ORDER BY rowid",
        )
        announced = [row["content"] for row in journal if row["role"] == "event"]
        assert announced.count("[matrix] porte ouverte") == 1
        assert announced.count("[planificateur] sortir les poubelles") == 1

    asyncio.run(scenario())
