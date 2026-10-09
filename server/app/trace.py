from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TextIO

from server.core.ports import TraceEntry, TraceSink

_LEVEL_RANK = {
    "debug": 10,
    "info": 20,
    "warning": 30,
    "error": 40,
    "critical": 50,
}
_DEFAULT_RANK = _LEVEL_RANK["info"]


def _rank(level: str) -> int:
    return _LEVEL_RANK.get(level.lower(), _DEFAULT_RANK)


def render(entry: TraceEntry) -> str:
    """Rend une entrée structurée en une ligne de texte lisible."""
    timestamp = datetime.fromtimestamp(entry.timestamp, tz=timezone.utc)
    parts = [
        timestamp.isoformat(timespec="milliseconds"),
        f"{entry.level.upper():<8}",
        f"[{entry.source}]",
        entry.kind,
    ]
    if entry.payload:
        parts.append(
            json.dumps(
                dict(entry.payload),
                sort_keys=True,
                default=str,
                ensure_ascii=False,
            )
        )
    return " ".join(parts)


class ConsoleTraceSink:
    """Premier consommateur du flux : conserve les entrées et les rend en texte.

    Toutes les entrées sont conservées dans ``entries`` ; le rendu console est
    filtré par ``min_level`` pour que ``ATHENA_DEBUG_DECISIONS`` élève la
    verbosité sans jamais retirer d'événement du flux (ADR-0004).
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        min_level: str = "info",
    ) -> None:
        self._stream = sys.stderr if stream is None else stream
        self._min_level = _rank(min_level)
        self.entries: list[TraceEntry] = []

    def emit(self, entry: TraceEntry) -> None:
        self.entries.append(entry)
        if _rank(entry.level) >= self._min_level:
            self._stream.write(render(entry) + "\n")
            self._stream.flush()


def build_trace_sink(
    *,
    debug_decisions: bool = False,
    stream: TextIO | None = None,
) -> ConsoleTraceSink:
    """Construit le ``TraceSink`` console, plus verbeux en mode debug."""
    return ConsoleTraceSink(stream, min_level="debug" if debug_decisions else "info")


class _TraceLogHandler(logging.Handler):
    def __init__(self, sink: TraceSink) -> None:
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.emit(
            TraceEntry(
                timestamp=record.created,
                source="logging",
                kind=record.name,
                payload={"message": record.getMessage()},
                level=record.levelname.lower(),
            )
        )


def _logging_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.DEBUG)


def install_logging_bridge(
    sink: TraceSink, *, level: int | str = logging.DEBUG
) -> logging.Handler:
    """Branche le logging tiers sur le flux unique (ADR-0004).

    Les handlers racine existants sont remplacés pour qu'aucun log n'échappe au
    flux ; le handler retourné est le point d'entrée unique du logging. ``level``
    accepte un niveau numérique ou son nom (``"info"``).
    """
    handler = _TraceLogHandler(sink)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(_logging_level(level))
    return handler
