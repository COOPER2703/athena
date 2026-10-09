from __future__ import annotations

import time
from typing import Mapping

from server.core.ports import TraceEntry, TraceSink


class NullTraceSink:
    def emit(self, entry: TraceEntry) -> None:
        pass


def emit(
    sink: TraceSink,
    source: str,
    kind: str,
    *,
    payload: Mapping[str, object] | None = None,
    level: str = "info",
) -> None:
    sink.emit(
        TraceEntry(
            timestamp=time.time(),
            source=source,
            kind=kind,
            payload=dict(payload or {}),
            level=level,
        )
    )
