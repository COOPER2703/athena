from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Union


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
class LlmClosed:
    pass


@dataclass(frozen=True, slots=True)
class LlmFailed:
    cause: LlmFailureCause


@dataclass(frozen=True, slots=True)
class TimerFired:
    session_id: str = ""


Event = Union[
    ClientRegistered,
    SessionRequested,
    ClientAudio,
    ClientInterrupt,
    LlmOpened,
    LlmAudio,
    LlmTranscription,
    LlmClosed,
    LlmFailed,
    TimerFired,
]
