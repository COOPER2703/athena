from __future__ import annotations

from typing import Union

from protocol import (
    Audio,
    Interrupt,
    SessionEnd,
    SessionEnded,
    SessionStart,
    SessionStarted,
    ToolResult,
)
from server.core.commands import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    SendAudioToClient,
)
from server.core.events import (
    ClientAudio,
    ClientInterrupt,
    Event,
    SessionRequested,
)


def translate_inbound(client_id: str, message: object) -> Event | None:
    """Traduit un message Client → Serveur en Événement du noyau.

    Renvoie ``None`` pour les messages que l'adaptateur traite lui-même
    (``Register``, ``Ping``, ``Pong``) ou qui n'ont pas d'Événement noyau en T1
    (``ToolResult``, ``SessionEnd``).
    """
    if isinstance(message, Audio):
        return ClientAudio(data=message.data)
    if isinstance(message, SessionStart):
        return SessionRequested(client_id=client_id)
    if isinstance(message, Interrupt):
        return ClientInterrupt(client_id=client_id)
    return None


WireMessage = Union[Audio, SessionStarted, SessionEnded]


def outbound_message(command: object) -> tuple[str, WireMessage] | None:
    """Traduit une Commande de transport ciblée en (client_id, message wire).

    Renvoie ``None`` pour les Commandes qui ne sont pas du ressort du transport
    (connexion Live, envoi au LLM, minuteurs).
    """
    if isinstance(command, SendAudioToClient):
        return command.client_id, Audio(data=command.data)
    if isinstance(command, AnnounceSessionStarted):
        return command.client_id, SessionStarted(auto=command.auto)
    if isinstance(command, AnnounceSessionEnded):
        return command.client_id, SessionEnded(reason=command.reason)
    return None
