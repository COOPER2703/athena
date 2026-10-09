from __future__ import annotations

from typing import Mapping, NamedTuple, Protocol

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


class LlmSocket(Protocol):
    async def open(self, session_id: str) -> None: ...

    async def close(self, session_id: str) -> None: ...

    async def send_audio(self, session_id: str, data: bytes) -> None: ...


class Clock(Protocol):
    def schedule(self, session_id: str, timeout: float) -> None: ...

    def cancel(self, session_id: str) -> None: ...


class TraceEntry(NamedTuple):
    timestamp: float
    source: str
    kind: str
    payload: Mapping[str, object]
    level: str = "info"


class TraceSink(Protocol):
    def emit(self, entry: TraceEntry) -> None: ...
