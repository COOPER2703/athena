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
class SendUserTextToLlm:
    """Envoie l'entrée d'un Tour texte comme un tour utilisateur complet.

    Distincte de l'injection de mémoire (``turn_complete=False``) : ce contenu
    **déclenche** la réponse du modèle, une seule fois par Tour.
    """

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
class SendTextToTour:
    """Texte du Tour, poussé vers la destination qui l'a demandé (ADR-0023)."""

    tour_id: str
    text: str


@dataclass(frozen=True, slots=True)
class SendAudioToTour:
    """Audio du Tour, PCM 16 bits mono 24 kHz, relayé tel quel."""

    tour_id: str
    data: bytes


@dataclass(frozen=True, slots=True)
class SendToolCallToTour:
    """Un Tool appelé pendant le Tour, avec son issue (``ok`` ou ``error``).

    Le noyau ne transmet que le nom, les arguments et l'issue : jamais une
    valeur de Secret, ni le corps d'un résultat de plugin.
    """

    tour_id: str
    name: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    status: str = "ok"


@dataclass(frozen=True, slots=True)
class FinishTour:
    """Fin normale d'un Tour : ``completed``, ``canceled``, ``timeout`` ou ``closed``."""

    tour_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class FailTour:
    """Fin en échec d'un Tour, désignée par un code stable pour la surface."""

    tour_id: str
    code: str


@dataclass(frozen=True, slots=True)
class RefuseTour:
    """Le noyau est occupé : le Tour est refusé, sans mise en file."""

    tour_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RefuseSession:
    """Une Session a été demandée pendant un Tour : refus lisible par le Client.

    Le Protocole partagé reste inchangé : le transport traduit cette Commande en
    message ``Error`` existant, pour que le daemon affiche le motif.
    """

    client_id: str
    reason: str


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
    SendUserTextToLlm,
    SendAnnouncementToLlm,
    SendAudioToClient,
    SendToolResultToLlm,
    AnnounceSessionStarted,
    AnnounceSessionEnded,
    StartIdleTimer,
    CancelIdleTimer,
    SendTextToTour,
    SendAudioToTour,
    SendToolCallToTour,
    FinishTour,
    FailTour,
    RefuseTour,
    RefuseSession,
    Trace,
]
