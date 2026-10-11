from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Iterable

from server.core.ports import FactExtractor, JournalEntry, MemoryStore

RECENT_EVENT_ROLE = "event"


@dataclass(frozen=True, slots=True)
class ConsolidationReport:
    """Outcome of one Consolidation attempt (pure, traceable by the App)."""

    ran: bool
    since: datetime | None = None
    facts_extracted: int = 0
    facts_stored: int = 0
    duplicates: int = 0
    purged: int = 0
    transcript_chars: int = 0


class ConsolidationPolicy:
    """Politique de planification : créneaux fixes à heures locales (ADR-0009).

    Le planificateur ne connaît que des ``datetime`` injectés (heure locale du
    serveur) : ``due`` dit si un créneau a été franchi depuis la dernière
    Consolidation (avec rattrapage) et ``next_slot`` / ``delay_until_next``
    donnent l'échéance suivante. Aucune horloge implicite, donc testable avec une
    horloge injectée.
    """

    def __init__(self, hours: Iterable[int] = (0, 12)) -> None:
        normalized = tuple(sorted({int(hour) for hour in hours}))
        if not normalized:
            raise ValueError("ConsolidationPolicy needs at least one hour")
        for hour in normalized:
            if not 0 <= hour <= 23:
                raise ValueError(f"consolidation hours must be in [0, 23], got {hour}")
        self._hours = normalized

    @property
    def hours(self) -> tuple[int, ...]:
        return self._hours

    def _slots_on(self, day: datetime) -> list[datetime]:
        return [
            day.replace(hour=hour, minute=0, second=0, microsecond=0)
            for hour in self._hours
        ]

    def next_slot(self, now: datetime) -> datetime:
        """Next slot strictly after ``now`` (used to sleep until it)."""
        candidates = self._slots_on(now) + self._slots_on(now + timedelta(days=1))
        return min(slot for slot in candidates if slot > now)

    def delay_until_next(self, now: datetime) -> float:
        return max((self.next_slot(now) - now).total_seconds(), 0.0)

    def due(self, now: datetime, last_run: datetime | None) -> bool:
        """True when a slot has been crossed since ``last_run`` (catch-up)."""
        if last_run is None:
            return True
        return self.next_slot(self._align(last_run, now)) <= now

    @staticmethod
    def _align(moment: datetime, reference: datetime) -> datetime:
        if moment.tzinfo is None:
            return moment.replace(tzinfo=reference.tzinfo)
        return moment.astimezone(reference.tzinfo)


class Consolidation:
    """Lit le Journal depuis la dernière fois et écrit des faits durables.

    Exclut les tours d'Événement, déduplique par similarité avant d'écrire,
    purge le Journal au-delà de la rétention puis marque le passage. N'épingle
    jamais. La routine ne décide de rien seule : la ``ConsolidationPolicy`` dit
    si un créneau est dû.
    """

    def __init__(
        self,
        *,
        memory: MemoryStore,
        extractor: FactExtractor,
        policy: ConsolidationPolicy,
        user_id: str = "owner",
        dedup_threshold: float = 0.15,
        journal_retention_days: int = 3,
    ) -> None:
        self._memory = memory
        self._extractor = extractor
        self._policy = policy
        self._user_id = user_id
        self._dedup_threshold = dedup_threshold
        self._retention_days = journal_retention_days

    @property
    def policy(self) -> ConsolidationPolicy:
        return self._policy

    async def run(self, now: datetime) -> ConsolidationReport:
        last = await self._memory.last_consolidation(self._user_id)
        if not self._policy.due(now, last):
            return ConsolidationReport(ran=False, since=last)

        entries = await self._memory.journal_entries_since(self._user_id, last)
        transcript = self._transcript(entries)
        facts = await self._extractor.extract(transcript) if transcript else []

        stored = 0
        duplicates = 0
        for fact in facts:
            if not fact or not fact.strip():
                continue
            if await self._memory.is_duplicate(
                self._user_id, fact, self._dedup_threshold
            ):
                duplicates += 1
                continue
            await self._memory.append_fact(self._user_id, fact, pinned=False)
            stored += 1

        purged = await self._memory.purge_older_than(self._retention_days)
        await self._memory.mark_consolidated(self._user_id, now)

        return ConsolidationReport(
            ran=True,
            since=last,
            facts_extracted=len(facts),
            facts_stored=stored,
            duplicates=duplicates,
            purged=purged,
            transcript_chars=len(transcript),
        )

    @staticmethod
    def _transcript(entries: list[JournalEntry]) -> str:
        return "\n".join(
            f"[{entry.role}] {entry.text}"
            for entry in entries
            if entry.role != RECENT_EVENT_ROLE
        )


class ConsolidationScheduler:
    """Boucle d'arrière-plan : rattrape puis dort jusqu'au prochain créneau.

    Reçoit ``now`` et ``sleep`` injectés pour être testable sans horloge réelle.
    Tourne hors du chemin de traitement des Événements (App la lance comme tâche
    distincte) : une Consolidation en cours ne bloque jamais la parole.
    """

    def __init__(
        self,
        *,
        consolidation: Consolidation,
        now: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]],
        policy: ConsolidationPolicy | None = None,
        on_run: Callable[[ConsolidationReport], None] | None = None,
    ) -> None:
        self._consolidation = consolidation
        self._policy = policy or consolidation.policy
        self._now = now
        self._sleep = sleep
        self._on_run = on_run

    async def run(self) -> None:
        while True:
            report = await self._consolidation.run(self._now())
            if report.ran and self._on_run is not None:
                self._on_run(report)
            await self._sleep(self._policy.delay_until_next(self._now()))


def local_now() -> datetime:
    """Current time in the server's local timezone (fixed slots are local)."""
    return datetime.now().astimezone()
