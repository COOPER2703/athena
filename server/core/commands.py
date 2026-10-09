from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Union

from protocol import SessionEndReason


@dataclass(frozen=True, slots=True)
class OpenLiveConnection:
    session_id: str


@dataclass(frozen=True, slots=True)
class CloseLiveConnection:
    session_id: str


@dataclass(frozen=True, slots=True)
class SendAudioToLlm:
    session_id: str
    data: bytes


@dataclass(frozen=True, slots=True)
class SendAudioToClient:
    client_id: str
    data: bytes


@dataclass(frozen=True, slots=True)
class AnnounceSessionStarted:
    client_id: str
    auto: bool = False


@dataclass(frozen=True, slots=True)
class AnnounceSessionEnded:
    client_id: str
    reason: SessionEndReason


@dataclass(frozen=True, slots=True)
class StartIdleTimer:
    session_id: str
    timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class CancelIdleTimer:
    session_id: str


@dataclass(frozen=True, slots=True)
class Trace:
    source: str
    kind: str
    payload: Mapping[str, object] = field(default_factory=dict)
    level: str = "info"


Command = Union[
    OpenLiveConnection,
    CloseLiveConnection,
    SendAudioToLlm,
    SendAudioToClient,
    AnnounceSessionStarted,
    AnnounceSessionEnded,
    StartIdleTimer,
    CancelIdleTimer,
    Trace,
]
