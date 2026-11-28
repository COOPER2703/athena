"""Une Planification ponctuelle sonne de bout en bout (ADR-0028, ticket #195).

Couture de composition : le vrai serveur (``build_app``, Console, transport,
mémoire SQLite), le faux modèle Live et deux coutures de temps — l'horloge
murale (création et pump) et le réveil. Aucun sommeil réel : le test avance
l'heure simulée, réveille le pump, puis observe l'Annonce.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from protocol import Audio
from server.adapters.memory import SqliteMemoryStore
from server.core import (
    PLANNED_REMINDER_TRIGGER,
    Consolidation,
    ConsolidationPolicy,
)
from server_harness import FakeEmbedder, running_server


class ManualWallClock:
    """Horloge murale aware, avancée explicitement par le test."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


class ManualWakeup:
    """Réveil piloté : ``wait`` ne rend la main qu'à ``release``/``notify``."""

    def __init__(self) -> None:
        self.waits: list[float | None] = []
        self._release = asyncio.Event()

    def notify(self) -> None:
        self._release.set()

    async def wait(self, timeout: float | None = None) -> None:
        self.waits.append(timeout)
        await self._release.wait()
        self._release.clear()

    def release(self) -> None:
        self._release.set()


class RecordingExtractor:
    """Extracteur de Consolidation : retient le transcript reçu, n'écrit rien."""

    def __init__(self) -> None:
        self.transcripts: list[str] = []

    async def extract(self, transcript: str) -> list[str]:
        self.transcripts.append(transcript)
        return []


def test_a_one_shot_reminder_is_announced_at_the_planned_simulated_second(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = ManualWallClock(datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc))
        wakeup = ManualWakeup()
        async with running_server(
            tmp_path,
            wall_clock=clock,
            wakeup=wakeup,
            env={"ATHENA_TIMEZONE": "UTC"},
        ) as server:
            await server.install()
            async with server.client() as client:
                created = await server.app.tools.execute(
                    "create_schedule",
                    {
                        "content": "appeler le plombier",
                        "when": {"kind": "once", "delay_seconds": 600},
                    },
                )
                assert created["status"] == "ok"
                assert created["next_occurrence"].startswith("2026-06-01T10:10:00")

                # Le pump dort exactement jusqu'à l'échéance annoncée.
                await server.wait_until(lambda: 600.0 in wakeup.waits)
                assert server.model.received_texts == []

                # Le modèle répond au tour d'Annonce.
                server.model.reply(
                    text="C'est l'heure d'appeler le plombier.",
                    audio=b"rappel",
                )

                # La seconde prévue n'est pas encore atteinte : rien ne sonne.
                wakeup.release()
                await asyncio.sleep(0)
                assert server.model.received_texts == []

                clock.advance(600)
                wakeup.release()
                # La fenêtre d'Occurrence est armée à zéro seconde : son
                # échéance est le même instant simulé, jamais +5 s.
                await server.wait_until(
                    lambda: "event-window" in server.clock.scheduled
                )
                server.clock.advance(0)

                message = await client.wait_message(Audio, timeout=2.0)
                assert message.data == b"rappel"

                prompts = server.model.received_texts
                assert prompts[-1] == PLANNED_REMINDER_TRIGGER
                assert "appeler le plombier" not in prompts[-1]
                facts = "\n".join(prompts[:-1])
                assert "[planificateur] appeler le plombier" in facts
                # Le contexte temporel reste injecté dans l'instruction.
                assert "[CONTEXTE TEMPOREL]" in server.model.instruction()

            # La ponctuelle est effacée dès l'émission.
            rows = server.query(
                server.paths.schedule_db, "SELECT COUNT(*) AS n FROM schedules"
            )
            assert rows[0]["n"] == 0

        # Le fait annoncé est au Journal en rôle ``event`` : exclu de la
        # Consolidation par le rôle établi (ADR-0007).
        journal = server.query(
            server.paths.memory_db,
            "SELECT role, content FROM journal ORDER BY rowid",
        )
        announced = [row["content"] for row in journal if row["role"] == "event"]
        assert "[planificateur] appeler le plombier" in announced

        # La Consolidation ne voit aucun de ces tours : son transcript est vide
        # alors que le Journal, lui, porte bien l'Annonce.
        memory = SqliteMemoryStore(
            db_path=str(server.paths.memory_db), embedder=FakeEmbedder()
        )
        extractor = RecordingExtractor()
        consolidation = Consolidation(
            memory=memory,
            extractor=extractor,
            policy=ConsolidationPolicy((0,)),
            user_id="owner",
        )
        report = await consolidation.run(
            datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)
        )
        assert report.ran
        assert report.transcript_chars == 0
        assert extractor.transcripts == []

    asyncio.run(scenario())


def test_create_schedule_uses_the_instance_timezone_setting(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = ManualWallClock(datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc))
        async with running_server(
            tmp_path,
            wall_clock=clock,
            env={"ATHENA_TIMEZONE": "Europe/Paris"},
        ) as server:
            created = await server.app.tools.execute(
                "create_schedule",
                {
                    "content": "point hebdo",
                    "when": {"kind": "daily", "time": "09:00"},
                },
            )

            # 12 h à Paris : le prochain 9 h local est le lendemain, 7 h UTC.
            assert created["status"] == "ok"
            assert created["next_occurrence"] == "2026-06-02T09:00:00+02:00"

    asyncio.run(scenario())


def test_the_pump_does_not_exist_during_installation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as server:
            assert server.app.planificateur is None
            # Les Tools restent utilisables : la Planification persiste, sans
            # aucune émission (ADR-0028).
            created = await server.app.tools.execute(
                "create_schedule",
                {
                    "content": "appeler le plombier",
                    "when": {"kind": "once", "delay_seconds": 60},
                },
            )
            assert created["status"] == "ok"

    asyncio.run(scenario())
