"""Une Planification sonne de bout en bout, y compris après un arrêt (ADR-0028).

Couture de composition : le vrai serveur (``build_app``, Console, transport,
mémoire SQLite), le faux modèle Live et deux coutures de temps — l'horloge
murale (création et pump) et le réveil. Aucun sommeil réel : le test avance
l'heure simulée, réveille le pump, puis observe l'Annonce. Le rattrapage d'une
ponctuelle, la reprise sans rafale d'une récurrente et l'activation à chaud de
la fin d'Installation sont couverts (#196).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from protocol import Audio
from server.adapters.console.contract import SECRETS_PATH
from server.adapters.memory import SqliteMemoryStore
from server.adapters.schedule import SqliteScheduleStore
from server.core import (
    PLANNED_REMINDER_TRIGGER,
    Consolidation,
    ConsolidationPolicy,
)
from server.core.events import OccurrenceReceived
from server.core.ports import Schedule, ScheduleRule
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


# ---------------------------------------------------------------------------
# Occurrences manquées : rattrapage et reprise (#196)
# ---------------------------------------------------------------------------


def _seed_schedule(
    paths,
    *,
    schedule_id: str,
    content: str,
    next_occurrence: int,
    rule: ScheduleRule,
) -> None:
    """Écrit une Planification avant le démarrage, comme une base déjà remplie."""
    store = SqliteScheduleStore(db_path=str(paths.schedule_db))
    store.create(
        Schedule(
            id=schedule_id,
            content=content,
            rule=rule,
            next_occurrence=next_occurrence,
            created_at=next_occurrence - 3600,
            updated_at=next_occurrence - 3600,
        )
    )


def _stored_next_occurrence(server, schedule_id: str) -> int | None:
    rows = server.query(
        server.paths.schedule_db,
        "SELECT next_occurrence FROM schedules WHERE id = ?",
        (schedule_id,),
    )
    return None if not rows else int(rows[0]["next_occurrence"])


def _schedule_count(server) -> int:
    rows = server.query(
        server.paths.schedule_db, "SELECT COUNT(*) AS n FROM schedules"
    )
    return int(rows[0]["n"])


def test_a_closer_schedule_wakes_the_pump_before_the_old_deadline(
    tmp_path: Path,
) -> None:
    """Un rappel créé plus proche ne subit pas l'échéance déjà attendue."""

    async def scenario() -> None:
        clock = ManualWallClock(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
        wakeup = ManualWakeup()
        async with running_server(
            tmp_path, wall_clock=clock, wakeup=wakeup, env={"ATHENA_TIMEZONE": "UTC"}
        ) as server:
            await server.install()
            first = await server.app.tools.execute(
                "create_schedule",
                {
                    "content": "rappel lointain",
                    "when": {"kind": "once", "delay_seconds": 600},
                },
            )
            assert first["status"] == "ok"
            await server.wait_until(lambda: 600.0 in wakeup.waits)

            second = await server.app.tools.execute(
                "create_schedule",
                {
                    "content": "rappel proche",
                    "when": {"kind": "once", "delay_seconds": 60},
                },
            )
            assert second["status"] == "ok"

            # Le réveil de la création fait recalculer l'échéance la plus proche.
            await server.wait_until(lambda: wakeup.waits[-1] == 60.0)

    asyncio.run(scenario())


def test_a_one_shot_missed_before_startup_is_caught_up_once(tmp_path: Path) -> None:
    """La ponctuelle écrite dans la base avant le runtime sonne une fois, en retard."""

    async def scenario() -> None:
        clock = ManualWallClock(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
        due = int((clock.moment - timedelta(hours=2)).timestamp())

        def prepare(paths) -> None:
            _seed_schedule(
                paths,
                schedule_id="sch_past",
                content="appeler le plombier",
                next_occurrence=due,
                rule=ScheduleRule(kind="once", at=due),
            )

        async with running_server(
            tmp_path,
            wall_clock=clock,
            prepare=prepare,
            env={"ATHENA_TIMEZONE": "UTC"},
        ) as server:
            await server.install()
            server.model.reply(
                text="C'est l'heure d'appeler le plombier.", audio=b"rappel"
            )

            # Le pump de démarrage réclame et efface la ponctuelle avant même
            # qu'un Client ne soit connecté : l'Occurrence patiente en File.
            await server.wait_until(lambda: _schedule_count(server) == 0)

            async with server.trace_stream() as stream:
                emitted = stream.find("OccurrenceEmitted", source="planificateur")

            assert emitted is not None
            assert emitted["payload"] == {
                "schedule_id": "sch_past",
                "kind": "once",
                "due": due,
            }

            async with server.client() as client:
                await server.wait_until(
                    lambda: "event-window" in server.clock.scheduled
                )
                server.clock.advance(0)
                message = await client.wait_message(Audio, timeout=2.0)
                assert message.data == b"rappel"

            prompts = server.model.received_texts
            assert prompts[-1] == PLANNED_REMINDER_TRIGGER
            assert "[planificateur] appeler le plombier" in "\n".join(prompts[:-1])

    asyncio.run(scenario())


def test_a_recurring_miss_during_downtime_is_skipped_once_and_resumes(
    tmp_path: Path,
) -> None:
    """Aucune rafale : un saut tracé, puis l'Annonce au créneau suivant."""

    async def scenario() -> None:
        clock = ManualWallClock(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
        # Trois créneaux quotidiens (29, 30, 31 mai, 1er juin à 09:00) sont
        # derrière nous : la récurrence doit reprendre le 2 juin, pas les rejouer.
        missed_due = int(
            datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc).timestamp()
        )
        next_slot = int(
            datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc).timestamp()
        )
        wakeup = ManualWakeup()

        def prepare(paths) -> None:
            _seed_schedule(
                paths,
                schedule_id="sch_daily",
                content="point quotidien",
                next_occurrence=missed_due,
                rule=ScheduleRule(kind="daily", time_of_day="09:00"),
            )

        async with running_server(
            tmp_path,
            wall_clock=clock,
            wakeup=wakeup,
            prepare=prepare,
            env={"ATHENA_TIMEZONE": "UTC"},
        ) as server:
            await server.install()

            async with server.trace_stream() as stream:
                # Une seule Trace pour la Planification, pas une par créneau.
                await server.wait_until(
                    lambda: _stored_next_occurrence(server, "sch_daily")
                    == next_slot
                )
                skipped = [
                    trace
                    for trace in stream.backlog
                    if trace["kind"] == "OccurrenceSkipped"
                    and trace["source"] == "planificateur"
                ]
                emitted = [
                    trace
                    for trace in stream.backlog
                    if trace["kind"] == "OccurrenceEmitted"
                ]

            assert skipped == [
                {
                    "timestamp": skipped[0]["timestamp"],
                    "level": "info",
                    "source": "planificateur",
                    "kind": "OccurrenceSkipped",
                    "payload": {
                        "schedule_id": "sch_daily",
                        "kind": "daily",
                        "reason": "missed_slots",
                        "due": missed_due,
                        "now": int(clock.moment.timestamp()),
                    },
                }
            ]
            assert emitted == []
            # Le pump dort exactement jusqu'au prochain créneau valide.
            assert wakeup.waits[-1] == float(next_slot - int(clock.moment.timestamp()))
            # Rien n'a été annoncé pendant cette attente.
            assert server.model.received_texts == []

            server.model.reply(text="Point quotidien.", audio=b"quotidien")
            async with server.client() as client:
                clock.advance(next_slot - int(clock.moment.timestamp()))
                wakeup.release()
                await server.wait_until(
                    lambda: "event-window" in server.clock.scheduled
                )
                server.clock.advance(0)
                message = await client.wait_message(Audio, timeout=2.0)
                assert message.data == b"quotidien"

            assert server.model.received_texts[-1] == PLANNED_REMINDER_TRIGGER
            # Une seule Annonce, et la récurrence reste au prochain jour.
            assert _stored_next_occurrence(server, "sch_daily") == next_slot + 86_400

    asyncio.run(scenario())


def test_a_one_shot_expired_during_installation_is_caught_up_at_activation(
    tmp_path: Path,
) -> None:
    """Aucun pump pendant l'Installation ; la première clé rattrape à chaud."""

    async def scenario() -> None:
        clock = ManualWallClock(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
        wakeup = ManualWakeup()
        async with running_server(
            tmp_path,
            gemini_key="",
            wall_clock=clock,
            wakeup=wakeup,
            env={"ATHENA_TIMEZONE": "UTC"},
        ) as server:
            http = await server.install()
            assert server.app.planificateur is None

            created = await server.app.tools.execute(
                "create_schedule",
                {
                    "content": "appeler le plombier",
                    "when": {"kind": "once", "delay_seconds": 600},
                },
            )
            assert created["status"] == "ok"
            due = int((clock.moment + timedelta(minutes=10)).timestamp())

            # L'heure passe pendant l'Installation, sans aucune émission.
            clock.advance(3600)
            assert server.model.received_texts == []
            assert _schedule_count(server) == 1

            async with http.put(
                f"{server.url}{SECRETS_PATH}/GEMINI_API_KEY",
                json={"value": "cle-gemini-de-test-196"},
            ) as response:
                assert response.status == 200, await response.text()

            assert server.app.planificateur is not None
            server.model.reply(
                text="C'est l'heure d'appeler le plombier.", audio=b"rappel"
            )

            async with server.client() as client:
                # Le pump né de l'activation rattrape la ponctuelle expirée.
                await server.wait_until(lambda: _schedule_count(server) == 0)
                await server.wait_until(
                    lambda: "event-window" in server.clock.scheduled
                )
                server.clock.advance(0)
                message = await client.wait_message(Audio, timeout=2.0)
                assert message.data == b"rappel"

            async with server.trace_stream() as stream:
                emitted = stream.find("OccurrenceEmitted", source="planificateur")
                activated = stream.find("RuntimeActivated")

            assert emitted is not None
            assert emitted["payload"] == {
                "schedule_id": created["id"],
                "kind": "once",
                "due": due,
            }
            assert activated is not None

    asyncio.run(scenario())


def test_a_recurring_expired_during_installation_resumes_at_activation(
    tmp_path: Path,
) -> None:
    """À l'activation, la récurrente saute les créneaux ratés sans rafale."""

    async def scenario() -> None:
        clock = ManualWallClock(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc))
        wakeup = ManualWakeup()
        async with running_server(
            tmp_path,
            gemini_key="",
            wall_clock=clock,
            wakeup=wakeup,
            env={"ATHENA_TIMEZONE": "UTC"},
        ) as server:
            http = await server.install()
            created = await server.app.tools.execute(
                "create_schedule",
                {
                    "content": "point quotidien",
                    "when": {"kind": "daily", "time": "09:00"},
                },
            )
            assert created["status"] == "ok"
            assert created["next_occurrence"].startswith("2026-06-02T09:00:00")

            # Trois jours passent pendant l'Installation : deux créneaux dus.
            clock.advance(3 * 86_400)
            schedule_id = created["id"]

            async with http.put(
                f"{server.url}{SECRETS_PATH}/GEMINI_API_KEY",
                json={"value": "cle-gemini-de-test-196"},
            ) as response:
                assert response.status == 200, await response.text()

            next_slot = int(
                datetime(2026, 6, 5, 9, 0, tzinfo=timezone.utc).timestamp()
            )
            await server.wait_until(
                lambda: _stored_next_occurrence(server, schedule_id) == next_slot
            )
            # Le pump né de l'activation attend le prochain créneau, 21 h plus tard.
            await server.wait_until(lambda: wakeup.waits[-1] == 75_600.0)
            assert server.model.received_texts == []

            async with server.trace_stream() as stream:
                skipped = [
                    trace
                    for trace in stream.backlog
                    if trace["kind"] == "OccurrenceSkipped"
                    and trace["payload"].get("schedule_id") == schedule_id
                ]
                emitted = [
                    trace
                    for trace in stream.backlog
                    if trace["kind"] == "OccurrenceEmitted"
                ]

            assert len(skipped) == 1
            assert skipped[0]["payload"] == {
                "schedule_id": schedule_id,
                "kind": "daily",
                "reason": "missed_slots",
                "due": int(
                    datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc).timestamp()
                ),
                "now": int(clock.moment.timestamp()),
            }
            assert emitted == []

            async with server.client():
                await asyncio.sleep(0)
                assert "event-window" not in server.clock.scheduled
            assert server.model.received_texts == []

    asyncio.run(scenario())


def test_the_trace_stream_reports_a_dropped_occurrence(tmp_path: Path) -> None:
    """Un abandon du noyau est une Trace de la Console, pas un silence."""

    async def scenario() -> None:
        from server.core import EVENT_MAX_CHARS

        async with running_server(tmp_path) as server:
            await server.install()
            async with server.trace_stream() as stream:
                server.app.push_occurrence(
                    OccurrenceReceived(
                        content="x" * (EVENT_MAX_CHARS + 1),
                        schedule_id="sch_too_large",
                    )
                )
                trace = await stream.next_trace("OccurrenceDropped")

            assert trace["payload"]["reason"] == "too_large"
            assert trace["payload"]["schedule_id"] == "sch_too_large"

    asyncio.run(scenario())

