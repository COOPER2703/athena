from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Union

from protocol import SessionEndReason

from server.core.ports import ToolSpec


@dataclass(frozen=True, slots=True)
class OpenLiveConnection:
    connection_id: str
    instruction: str = ""
    tools: tuple[ToolSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class CloseLiveConnection:
    connection_id: str


@dataclass(frozen=True, slots=True)
class SendAudioToLlm:
    connection_id: str
    data: bytes


@dataclass(frozen=True, slots=True)
class SendMemoryToLlm:
    connection_id: str
    text: str


@dataclass(frozen=True, slots=True)
class SendJudgeBatchToLlm:
    """Envoie un lot d'Événements à la Connexion Live de jugement.

    Commande distincte de l'injection de mémoire (ADR-0007) : le lot est
    transmis comme contenu de tour, jamais comme rappel mémoriel.
    """

    connection_id: str
    text: str


@dataclass(frozen=True, slots=True)
class SendAudioToClient:
    client_id: str
    data: bytes


@dataclass(frozen=True, slots=True)
class SendToolResultToLlm:
    connection_id: str
    tool_id: str
    name: str
    result: Mapping[str, object]


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
    connection_id: str
    timeout: float


@dataclass(frozen=True, slots=True)
class CancelIdleTimer:
    connection_id: str


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
    SendMemoryToLlm,
    SendJudgeBatchToLlm,
    SendAudioToClient,
    SendToolResultToLlm,
    AnnounceSessionStarted,
    AnnounceSessionEnded,
    StartIdleTimer,
    CancelIdleTimer,
    Trace,
]
