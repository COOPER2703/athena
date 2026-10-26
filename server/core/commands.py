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
class SendAnnouncementToLlm:
    """Fait prononcer au modèle Live une Annonce proactive (ADR-0007, amendé).

    Le verdict (parler ou se taire) vient du jugement **texte** ; cette commande
    porte à la fois la phrase à prononcer (``text``, le message du juge) et les
    faits bruts de l'Événement (``facts``). Les faits entrent dans le contexte du
    Live, sans être prononcés, pour qu'Athena puisse détailler l'Événement quand
    l'utilisateur le lui demande (ADR-0013). Distincte de l'injection de mémoire.
    """

    connection_id: str
    text: str
    facts: str = ""


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
    SendAnnouncementToLlm,
    SendAudioToClient,
    SendToolResultToLlm,
    AnnounceSessionStarted,
    AnnounceSessionEnded,
    StartIdleTimer,
    CancelIdleTimer,
    Trace,
]
