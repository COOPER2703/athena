from __future__ import annotations

from typing import Union

from protocol import (
    Audio,
    Interrupt,
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
    ClientToolResult,
    Event,
    SessionRequested,
)


# Reserved seams — intentional T1 deferrals, not omissions.
#
# Outbound ``Text`` and ``Error``: the spec lists them as server → client
# messages, but no core Command produces them in T1. ``Error`` is emitted by
# the adapter itself on a rejected Register (transport-originated), and the
# desktop client ignores ``Text``. Both stay deferred until a core Command
# produces them; ``outbound_message`` therefore maps none of them.
#
# ``ToolCall`` / client Tools: no cross-client Tool routing exists in T1, so
# ``ClientLink.send_tool_call`` is a reserved no-op and ``client_tools`` stays
# empty. The inbound counterpart is the reserved ``ClientToolResult`` event.
#
# Broadcast: core is single-client in T1 (one visible Session), so every
# outbound Command is targeted by ``client_id``. Any broadcast is an adapter
# detail, deliberately not modelled in the core and deferred until needed.
#
# Unknown or non-transport Commands are silently ignored (no wire message, no
# crash): execute() must never fail the connection on a Command it does not own.


def translate_inbound(client_id: str, message: object) -> Event | None:
    """Traduit un message Client → Serveur en Événement du noyau.

    Renvoie ``None`` pour les messages que l'adaptateur traite lui-même
    (``Register``, ``Ping``, ``Pong``) ou qui n'ont pas d'Événement noyau en T1
    (``SessionEnd``).

    ``ToolResult`` est traduit vers ``ClientToolResult``, un seam réservé : le
    noyau l'accepte et le trace, mais aucun routage de Tool n'existe encore.
    """
    if isinstance(message, Audio):
        return ClientAudio(data=message.data)
    if isinstance(message, SessionStart):
        return SessionRequested(client_id=client_id)
    if isinstance(message, Interrupt):
        return ClientInterrupt(client_id=client_id)
    if isinstance(message, ToolResult):
        return ClientToolResult(
            tool_id=message.id, name=message.name, result_json=message.result_json
        )
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
