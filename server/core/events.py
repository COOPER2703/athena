from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Union


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class LlmFailureCause(StrEnum):
    ERROR = "error"
    GO_AWAY = "go_away"


@dataclass(frozen=True, slots=True)
class ClientRegistered:
    client_id: str
    client_name: str = ""
    platform: str = ""


@dataclass(frozen=True, slots=True)
class SessionRequested:
    client_id: str
    auto: bool = False


@dataclass(frozen=True, slots=True)
class ClientAudio:
    data: bytes


@dataclass(frozen=True, slots=True)
class ClientInterrupt:
    client_id: str = ""


@dataclass(frozen=True, slots=True)
class ClientToolResult:
    tool_id: str = ""
    name: str = ""
    result_json: bytes = b""


@dataclass(frozen=True, slots=True)
class LlmOpened:
    pass


@dataclass(frozen=True, slots=True)
class LlmAudio:
    data: bytes


@dataclass(frozen=True, slots=True)
class LlmTranscription:
    text: str
    role: Role


@dataclass(frozen=True, slots=True)
class LlmToolCall:
    tool_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class LlmClosed:
    connection_id: str = ""


@dataclass(frozen=True, slots=True)
class LlmFailed:
    cause: LlmFailureCause
    connection_id: str = ""


@dataclass(frozen=True, slots=True)
class ClientDisconnected:
    client_id: str


@dataclass(frozen=True, slots=True)
class ShutdownRequested:
    pass


@dataclass(frozen=True, slots=True)
class TimerFired:
    connection_id: str


@dataclass(frozen=True, slots=True)
class EventReceived:
    """Un Événement externe poussé par une Source (ADR-0007/0010).

    Le mot « Événement » reste réservé au fait externe ; il n'est jamais
    injecté dans une Session active, seulement jugé au Repos.
    """

    source: str
    content: str


Event = Union[
    ClientRegistered,
    SessionRequested,
    ClientAudio,
    ClientInterrupt,
    ClientToolResult,
    LlmOpened,
    LlmAudio,
    LlmTranscription,
    LlmToolCall,
    LlmClosed,
    LlmFailed,
    ClientDisconnected,
    ShutdownRequested,
    TimerFired,
    EventReceived,
]
