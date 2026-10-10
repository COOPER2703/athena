from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Callable, Mapping

from server.adapters.gemini.translation import translate_response
from server.adapters.trace import NullTraceSink, emit
from server.core.commands import (
    CloseLiveConnection,
    Command,
    OpenLiveConnection,
    SendAudioToLlm,
    Trace,
)
from server.core.events import (
    Event,
    LlmClosed,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
)
from server.core.ports import TraceSink

_TRACE_SOURCE = "gemini"
_UPSTREAM_MIME_TYPE = "audio/pcm;rate=16000"

LiveConnect = Callable[[str, Any], Any]


def _default_live_connect(api_key: str) -> LiveConnect:
    def connect(model: str, config: Any) -> Any:
        from google import genai

        client = genai.Client(api_key=api_key)
        return client.aio.live.connect(model=model, config=config)

    return connect


class GeminiLiveAdapter:
    """Adaptateur Gemini Live : Connexion Live ↔ Événements du domaine.

    Implémente le port ``LlmSocket``. Il ouvre/ferme une Connexion Live via le
    ``live_connect`` injecté, envoie l'audio montant et remonte l'audio
    descendant, les transcriptions et les défaillances sous forme d'Événements
    déposés dans une file (``recv``). Aucune politique de Session : le noyau
    seul décide.

    L'observabilité passe exclusivement par le ``TraceSink`` injecté (ADR-0004).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        live_connect: LiveConnect | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._model = model
        self._voice = voice
        self._live_connect: LiveConnect = live_connect or _default_live_connect(api_key)
        self._trace_sink: TraceSink = trace_sink or NullTraceSink()
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._context: Any | None = None
        self._live: Any | None = None
        self._connection_id = ""
        self._receiver: asyncio.Task[None] | None = None
        self._terminated = False

    async def recv(self) -> Event:
        return await self._events.get()

    async def execute(self, command: Command) -> None:
        if isinstance(command, Trace):
            emit(
                self._trace_sink,
                command.source,
                command.kind,
                payload=command.payload,
                level=command.level,
            )
            return
        if isinstance(command, OpenLiveConnection):
            await self.open(command.connection_id)
            return
        if isinstance(command, CloseLiveConnection):
            await self.close(command.connection_id)
            return
        if isinstance(command, SendAudioToLlm):
            await self.send_audio(command.connection_id, command.data)
            return

    async def open(self, connection_id: str) -> None:
        if self._receiver is not None or self._context is not None:
            await self._shutdown()
        self._connection_id = connection_id
        self._terminated = False
        try:
            config = self._build_config()
            self._context = self._live_connect(self._model, config)
            self._live = await self._context.__aenter__()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._context = None
            self._live = None
            self._emit(
                "connection_failed",
                payload={"connection_id": connection_id, "error": str(exc)},
                level="error",
            )
            await self._fail(LlmFailureCause.ERROR)
            return
        self._receiver = asyncio.create_task(self._receive_loop(self._live))
        self._emit(
            "connected",
            payload={
                "connection_id": connection_id,
                "model": self._model,
                "voice": self._voice,
            },
        )
        await self._events.put(LlmOpened())

    async def _receive_loop(self, live: Any) -> None:
        try:
            # ``receive()`` yields one complete model turn and returns while the
            # connection stays open, so a Session spans many turns; keep pulling
            # turns until the stream ends or the connection terminally fails.
            while not self._terminated:
                delivered = False
                async for response in live.receive():
                    delivered = True
                    await self._handle_response(response)
                    if self._terminated:
                        break
                if not delivered:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit("receive_error", payload={"error": str(exc)}, level="error")
            await self._fail(LlmFailureCause.ERROR)
        else:
            await self._close_once()

    async def _handle_response(self, response: Any) -> None:
        go_away = getattr(response, "go_away", None)
        if go_away is not None:
            self._emit(
                "go_away",
                payload={"time_left": str(getattr(go_away, "time_left", ""))},
                level="warning",
            )
        elif getattr(response, "tool_call", None) is not None:
            self._emit(
                "tool_call_ignored",
                payload={"connection_id": self._connection_id},
                level="warning",
            )

        for event in translate_response(response):
            if isinstance(event, LlmFailed):
                await self._fail(event.cause)
            else:
                await self._events.put(event)

    async def _fail(self, cause: LlmFailureCause) -> None:
        if self._terminated:
            return
        self._terminated = True
        await self._events.put(LlmFailed(cause=cause))

    async def close(self, connection_id: str) -> None:
        await self._shutdown()
        await self._close_once()

    async def _shutdown(self) -> None:
        receiver = self._receiver
        self._receiver = None
        if receiver is not None and not receiver.done():
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await receiver

        context = self._context
        self._context = None
        self._live = None
        if context is None:
            return
        try:
            await context.__aexit__(None, None, None)
        except Exception as exc:
            self._emit(
                "disconnect_error", payload={"error": str(exc)}, level="warning"
            )
        self._emit("disconnected", payload={"connection_id": self._connection_id})

    async def _close_once(self) -> None:
        if self._terminated:
            return
        self._terminated = True
        await self._events.put(LlmClosed())

    async def send_audio(self, connection_id: str, data: bytes) -> None:
        live = self._live
        if live is None or self._terminated:
            self._emit(
                "send_audio_ignored",
                payload={"connection_id": connection_id},
                level="warning",
            )
            return
        try:
            from google.genai import types

            await live.send_realtime_input(
                audio=types.Blob(data=data, mime_type=_UPSTREAM_MIME_TYPE)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit("send_error", payload={"error": str(exc)}, level="error")
            await self._fail(LlmFailureCause.ERROR)

    def _build_config(self) -> Any:
        from google.genai import types

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=self._voice
                    )
                )
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

    def _emit(
        self,
        kind: str,
        *,
        payload: Mapping[str, object] | None = None,
        level: str = "info",
    ) -> None:
        emit(self._trace_sink, _TRACE_SOURCE, kind, payload=payload, level=level)
