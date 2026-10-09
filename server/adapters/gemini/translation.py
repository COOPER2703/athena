from __future__ import annotations

from server.core.events import (
    Event,
    LlmAudio,
    LlmFailed,
    LlmFailureCause,
    LlmTranscription,
    Role,
)


def translate_response(response: object) -> list[Event]:
    """Traduit une réponse Live Gemini en Événements du noyau.

    Un même message peut porter plusieurs champs : il produit alors plusieurs
    Événements (audio descendant ET transcription). Renvoie ``[]`` pour les
    messages sans Événement noyau en T1 (``tool_call`` est lu puis ignoré par
    l'adaptateur, qui le trace sans le traduire).
    """
    events: list[Event] = []

    data = getattr(response, "data", None)
    if data:
        events.append(LlmAudio(data=data))

    if getattr(response, "go_away", None) is not None:
        events.append(LlmFailed(cause=LlmFailureCause.GO_AWAY))

    server_content = getattr(response, "server_content", None)
    if server_content is not None:
        input_transcription = getattr(server_content, "input_transcription", None)
        if input_transcription is not None and getattr(
            input_transcription, "text", None
        ):
            events.append(
                LlmTranscription(text=input_transcription.text, role=Role.USER)
            )

        output_transcription = getattr(server_content, "output_transcription", None)
        if output_transcription is not None and getattr(
            output_transcription, "text", None
        ):
            events.append(
                LlmTranscription(text=output_transcription.text, role=Role.ASSISTANT)
            )

    return events
