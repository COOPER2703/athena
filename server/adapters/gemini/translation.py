from __future__ import annotations

from server.core.events import (
    Event,
    LlmAudio,
    LlmFailed,
    LlmFailureCause,
    LlmToolCall,
    LlmTranscription,
    Role,
)


def translate_response(response: object) -> list[Event]:
    """Traduit une réponse Live Gemini en Événements du noyau.

    Un même message peut porter plusieurs champs : il produit alors plusieurs
    Événements (audio descendant ET transcription ET appels de Tool). Renvoie
    ``[]`` pour les messages sans Événement noyau.
    """
    events: list[Event] = []

    data = getattr(response, "data", None)
    if data:
        events.append(LlmAudio(data=data))

    if getattr(response, "go_away", None) is not None:
        events.append(LlmFailed(cause=LlmFailureCause.GO_AWAY))

    tool_call = getattr(response, "tool_call", None)
    if tool_call is not None:
        for function_call in getattr(tool_call, "function_calls", None) or []:
            events.append(
                LlmToolCall(
                    tool_id=getattr(function_call, "id", None) or "",
                    name=getattr(function_call, "name", None) or "",
                    arguments=getattr(function_call, "args", None) or {},
                )
            )

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
