from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from server.core.ports import TraceEntry, TraceSink

_DEFAULT_LOG_FILENAME = "server.log"
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 3
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


class FileTraceSink:
    """Second consommateur du flux : écrit les entrées dans un fichier de log.

    Les agents de développement lisent ce fichier pour reconstituer la même
    chronologie que la console. Le fichier tourne par taille pour ne jamais
    grossir sans borne ; ``close`` referme le descripteur au shutdown.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        min_level: str = "info",
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_count: int = _DEFAULT_BACKUP_COUNT,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._min_level = _rank(min_level)
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._handle: TextIO | None = None

    @property
    def path(self) -> Path:
        return self._path

    def emit(self, entry: TraceEntry) -> None:
        if _rank(entry.level) < self._min_level:
            return
        line = render(entry) + "\n"
        handle = self._open()
        if self._should_rotate(len(line.encode("utf-8"))):
            self._rotate()
            handle = self._open()
        handle.write(line)
        handle.flush()

    def close(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.close()
        self._handle = None

    def _open(self) -> TextIO:
        if self._handle is None or self._handle.closed:
            self._handle = self._path.open("a", encoding="utf-8")
        return self._handle

    def _should_rotate(self, incoming: int) -> bool:
        if self._max_bytes <= 0:
            return False
        try:
            current = self._path.stat().st_size
        except FileNotFoundError:
            return False
        return current > 0 and current + incoming > self._max_bytes

    def _rotate(self) -> None:
        self.close()
        if self._backup_count <= 0:
            self._path.unlink(missing_ok=True)
            return
        for index in range(self._backup_count - 1, 0, -1):
            older = self._numbered(index)
            if older.exists():
                older.replace(self._numbered(index + 1))
        self._path.replace(self._numbered(1))

    def _numbered(self, index: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{index}")


class FanOutTraceSink:
    """Diffuse chaque entrée vers plusieurs consommateurs du flux unique."""

    def __init__(self, *sinks: TraceSink) -> None:
        self._sinks = sinks

    def emit(self, entry: TraceEntry) -> None:
        for sink in self._sinks:
            sink.emit(entry)

    def close(self) -> None:
        for sink in self._sinks:
            close = getattr(sink, "close", None)
            if close is not None:
                close()


def build_trace_sink(
    *,
    debug_decisions: bool = False,
    stream: TextIO | None = None,
    log_dir: str | Path | None = None,
    level: str = "info",
) -> FanOutTraceSink:
    """Construit les consommateurs du flux : console et, si demandé, fichier.

    Les deux consommateurs respectent la verbosité configurée (``LOG_LEVEL``) ;
    ``debug_decisions`` l'élève à ``debug`` sans créer de second canal. Le
    fichier conserve donc la même chronologie filtrée que la console.
    """
    min_level = "debug" if debug_decisions else level
    sinks: list[TraceSink] = [ConsoleTraceSink(stream, min_level=min_level)]
    if log_dir is not None:
        sinks.append(
            FileTraceSink(Path(log_dir) / _DEFAULT_LOG_FILENAME, min_level=min_level)
        )
    return FanOutTraceSink(*sinks)


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
