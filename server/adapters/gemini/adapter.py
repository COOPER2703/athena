from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Callable, Mapping

from server.core.commands import (
    CloseLiveConnection,
    Command,
    OpenLiveConnection,
    SendAudioToLlm,
    Trace,
)
from server.core.events import (
    Event,
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmFailureCause,
    LlmOpened,
    LlmTranscription,
    Role,
)
from server.core.ports import TraceEntry, TraceSink

_TRACE_SOURCE = "gemini"
_UPSTREAM_MIME_TYPE = "audio/pcm;rate=16000"

Connector = Callable[[str, Any], Any]


class _NullTraceSink:
    def emit(self, entry: TraceEntry) -> None:
        pass


def _default_connector(api_key: str) -> Connector:
    def connect(model: str, config: Any) -> Any:
        from google import genai

        client = genai.Client(api_key=api_key)
        return client.aio.live.connect(model=model, config=config)

    return connect


class GeminiLiveAdapter:
    """Adaptateur Gemini Live : Connexion Live ↔ Événements du domaine.

    Implémente le port ``LlmSocket``. Il ouvre/ferme une Connexion Live via le
    ``connector`` injecté, envoie l'audio montant et remonte l'audio descendant,
    les transcriptions et les défaillances sous forme d'Événements déposés dans
    une file (``recv``). Aucune politique de Session : le noyau seul décide.

    L'observabilité passe exclusivement par le ``TraceSink`` injecté (ADR-0004).
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        connector: Connector | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self._model = model
        self._voice = voice
        self._connector: Connector = connector or _default_connector(api_key)
        self._trace_sink: TraceSink = trace_sink or _NullTraceSink()
        self._events: asyncio.Queue[Event] = asyncio.Queue()
        self._context: Any | None = None
        self._session: Any | None = None
        self._connection_id = ""
        self._receiver: asyncio.Task[None] | None = None
        self._terminated = False

    async def recv(self) -> Event:
        return await self._events.get()

    async def execute(self, command: Command) -> None:
        if isinstance(command, Trace):
            self._trace_sink.emit(
                TraceEntry(
                    timestamp=time.time(),
                    source=command.source,
                    kind=command.kind,
                    payload=dict(command.payload),
                    level=command.level,
                )
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
        self._connection_id = connection_id
        self._terminated = False
        try:
            config = self._build_config()
            self._context = self._connector(self._model, config)
            self._session = await self._context.__aenter__()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._context = None
            self._session = None
            self._emit(
                "connection_failed",
                payload={"connection_id": connection_id, "error": str(exc)},
                level="error",
            )
            await self._fail(LlmFailureCause.ERROR)
            return
        self._receiver = asyncio.create_task(self._receive_loop(self._session))
        self._emit(
            "connected",
            payload={
                "connection_id": connection_id,
                "model": self._model,
                "voice": self._voice,
            },
        )
        await self._events.put(LlmOpened())

    async def _receive_loop(self, session: Any) -> None:
        try:
            async for response in session.receive():
                await self._handle_response(response)
                if self._terminated:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._emit("receive_error", payload={"error": str(exc)}, level="error")
            await self._fail(LlmFailureCause.ERROR)
        else:
            await self._close_once()

    async def _handle_response(self, response: Any) -> None:
        data = getattr(response, "data", None)
        if data:
            await self._events.put(LlmAudio(data=data))
            return

        go_away = getattr(response, "go_away", None)
        if go_away is not None:
            self._emit(
                "go_away",
                payload={"time_left": str(getattr(go_away, "time_left", ""))},
                level="warning",
            )
            await self._fail(LlmFailureCause.GO_AWAY)
            return

        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None:
            self._emit(
                "tool_call_ignored",
                payload={"connection_id": self._connection_id},
                level="warning",
            )
            return

        server_content = getattr(response, "server_content", None)
        if server_content is None:
            return

        input_transcription = getattr(server_content, "input_transcription", None)
        if input_transcription is not None and getattr(
            input_transcription, "text", None
        ):
            await self._events.put(
                LlmTranscription(text=input_transcription.text, role=Role.USER)
            )

        output_transcription = getattr(server_content, "output_transcription", None)
        if output_transcription is not None and getattr(
            output_transcription, "text", None
        ):
            await self._events.put(
                LlmTranscription(text=output_transcription.text, role=Role.ASSISTANT)
            )

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
        self._session = None
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
        session = self._session
        if session is None:
            self._emit(
                "send_audio_ignored",
                payload={"connection_id": connection_id},
                level="warning",
            )
            return
        try:
            from google.genai import types

            await session.send_realtime_input(
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
        self._trace_sink.emit(
            TraceEntry(
                timestamp=time.time(),
                source=_TRACE_SOURCE,
                kind=kind,
                payload=dict(payload or {}),
                level=level,
            )
        )
