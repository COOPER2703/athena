"""Faits de santé de l'instance, assemblés sans I/O (ticket #177).

Le bandeau du Diagnostic lit ce que la Console ne peut pas deviner : l'uptime,
la dernière Consolidation, la dernière erreur et la taille des bases. Ce module
en fixe la **forme** et retient les deux faits vivants, à partir de valeurs
injectées : aucune horloge implicite, aucun accès au système de fichiers, aucun
import interdit dans le noyau.

``HealthState`` est aussi un consommateur du flux unique de traces (il expose
``emit``) : la racine de composition l'ajoute aux sinks, et la dernière entrée
de niveau ``error`` devient « la dernière erreur ». Le payload d'une Trace n'est
**jamais** publié : un message d'erreur pourrait y porter un Secret.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from server.core.consolidation import ConsolidationReport
from server.core.ports import TraceEntry

#: Niveaux qui font d'une Trace « la dernière erreur ».
_ERROR_LEVELS = frozenset({"error", "critical"})

#: Ordre des bases publiées par le bandeau (les noms sont stables).
DATABASE_NAMES = ("memory", "auth", "settings")


@dataclass(frozen=True, slots=True)
class LastError:
    """Dernière Trace de niveau erreur, réduite à ce qui se lit d'un regard."""

    timestamp: float
    source: str
    kind: str
    level: str

    def as_json(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "source": self.source,
            "kind": self.kind,
            "level": self.level,
        }


@dataclass(frozen=True, slots=True)
class LastConsolidation:
    """Dernière Consolidation réellement exécutée, et son bilan."""

    at: float
    facts_extracted: int
    facts_stored: int
    duplicates: int
    purged: int
    transcript_chars: int

    def as_json(self) -> dict[str, object]:
        return {
            "at": self.at,
            "facts_extracted": self.facts_extracted,
            "facts_stored": self.facts_stored,
            "duplicates": self.duplicates,
            "purged": self.purged,
            "transcript_chars": self.transcript_chars,
        }


class HealthState:
    """Mémorise la dernière Consolidation et la dernière erreur.

    ``now`` est injecté par la racine de composition : le noyau ne lit jamais
    l'horloge murale lui-même. ``emit`` suit la couture ``TraceSink`` pour que
    cette mémoire soit branchée sur le fan-out existant, sans second canal.
    """

    def __init__(self, *, now: Callable[[], float]) -> None:
        self._now = now
        self._last_error: LastError | None = None
        self._last_consolidation: LastConsolidation | None = None

    def emit(self, entry: TraceEntry) -> None:
        """Retient la dernière erreur ; ignore tous les autres niveaux."""
        if entry.level not in _ERROR_LEVELS:
            return
        self._last_error = LastError(
            timestamp=float(entry.timestamp),
            source=str(entry.source),
            kind=str(entry.kind),
            level=str(entry.level),
        )

    def record_consolidation(self, report: ConsolidationReport) -> None:
        """Retient le bilan d'une Consolidation ; un passage vide n'écrase rien."""
        if not report.ran:
            return
        self._last_consolidation = LastConsolidation(
            at=self._now(),
            facts_extracted=report.facts_extracted,
            facts_stored=report.facts_stored,
            duplicates=report.duplicates,
            purged=report.purged,
            transcript_chars=report.transcript_chars,
        )

    @property
    def last_error(self) -> LastError | None:
        return self._last_error

    @property
    def last_consolidation(self) -> LastConsolidation | None:
        return self._last_consolidation


def health_snapshot(
    *,
    version: str,
    uptime_seconds: float,
    state: HealthState,
    databases: Mapping[str, int],
    trace_buffer: Mapping[str, int] | None = None,
) -> dict[str, object]:
    """Section ``health`` de l'instantané, prête pour le JSON.

    Tout est injecté : la fonction ne mesure rien elle-même. Les tailles et la
    borne du Tampon de traces viennent de l'adaptateur Console, l'uptime de
    l'horloge monotone de la racine. Aucun champ ne peut porter un Secret.
    """
    last_error = state.last_error
    last_consolidation = state.last_consolidation
    return {
        "version": str(version),
        "uptime_seconds": round(max(0.0, float(uptime_seconds)), 3),
        "last_consolidation": (
            None if last_consolidation is None else last_consolidation.as_json()
        ),
        "databases": {
            name: max(0, int(databases.get(name, 0)))
            for name in DATABASE_NAMES
        },
        "last_error": None if last_error is None else last_error.as_json(),
        "trace_buffer": (
            None
            if trace_buffer is None
            else {
                "size": max(0, int(trace_buffer.get("size", 0))),
                "capacity": max(0, int(trace_buffer.get("capacity", 0))),
            }
        ),
    }
