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
    # Une demande **non préemptante** (Console, ADR-0027) est refusée quand un
    # Tour ou une autre Session tient le noyau : la racine de composition
    # pré-vérifie, et ce drapeau ferme la course entre la vérification et
    # l'arrivée de l'événement.
    no_preempt: bool = False


@dataclass(frozen=True, slots=True)
class TourRequested:
    """Une entrée de **Tour** soumise par la Console (ADR-0024).

    Le Tour est l'unité de conversation écrite : une entrée, une Connexion Live
    ouverte pour sa durée, puis retour au Repos. Aucune Session n'est créée.
    """

    tour_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TourCanceled:
    """La surface a fermé le flux du Tour : le noyau l'interrompt.

    L'adaptateur Console ne touche jamais le Coordinator : il dépose cet
    Événement dans le Canal, exactement comme ``push_event``.
    """

    tour_id: str


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
    TourRequested,
    TourCanceled,
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
