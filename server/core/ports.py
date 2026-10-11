from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, NamedTuple, Protocol, Sequence

from protocol import SessionEndReason


class ClientLink(Protocol):
    async def send_audio(self, client_id: str, data: bytes) -> None: ...

    async def announce_session_started(self, client_id: str, auto: bool) -> None: ...

    async def announce_session_ended(
        self, client_id: str, reason: SessionEndReason
    ) -> None: ...

    async def send_tool_call(
        self, client_id: str, tool_id: str, name: str, arguments_json: bytes
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Pure description of a Tool a model may call (registry seam, issue #28)."""

    name: str
    description: str
    parameters: Mapping[str, object]


class LlmSocket(Protocol):
    async def open(
        self,
        session_id: str,
        instruction: str = "",
        tools: Sequence[ToolSpec] = (),
    ) -> None: ...

    async def close(self, session_id: str) -> None: ...

    async def send_audio(self, session_id: str, data: bytes) -> None: ...

    async def send_tool_result(
        self,
        session_id: str,
        tool_id: str,
        name: str,
        result: Mapping[str, object],
    ) -> None: ...


class Clock(Protocol):
    def schedule(self, connection_id: str, timeout: float) -> None: ...

    def cancel(self, connection_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """A candidate recalled from memory (pure; the kernel applies the policy)."""

    text: str
    distance: float
    source: str


class MemoryStore(Protocol):
    """Async memory port injected into the pure kernel (D2).

    ``session_context``/``append_turn`` are the ticket-#26 seam; ``recall``,
    ``save`` and ``forget`` are declared here so the adapter can grow into them
    without the kernel changing signature (issues #27/#29/#30).
    """

    async def session_context(self, user_id: str) -> str: ...

    async def append_turn(self, user_id: str, role: str, text: str) -> None: ...

    async def recall(self, user_id: str, query: str) -> list[MemoryHit]: ...

    async def save(self, user_id: str, content: str, *, pinned: bool) -> None: ...

    async def forget(
        self, user_id: str, query: str, max_distance: float
    ) -> bool: ...


class Embedder(Protocol):
    """Turns text into a vector, off the event loop (adapter owns ``to_thread``)."""

    async def embed(self, text: str) -> list[float]: ...


class TraceEntry(NamedTuple):
    timestamp: float
    source: str
    kind: str
    payload: Mapping[str, object]
    level: str = "info"


class TraceSink(Protocol):
    def emit(self, entry: TraceEntry) -> None: ...
