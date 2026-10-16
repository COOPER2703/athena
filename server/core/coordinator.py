from __future__ import annotations

from typing import Mapping

from protocol import SessionEndReason

from server.core.commands import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    CloseLiveConnection,
    Command,
    OpenLiveConnection,
    SendAudioToClient,
    SendAudioToLlm,
    SendMemoryToLlm,
    SendToolResultToLlm,
    StartIdleTimer,
    Trace,
)
from server.core.events import (
    ClientAudio,
    ClientDisconnected,
    ClientInterrupt,
    ClientRegistered,
    ClientToolResult,
    Event,
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmOpened,
    LlmToolCall,
    LlmTranscription,
    Role,
    SessionRequested,
    ShutdownRequested,
    TimerFired,
)
from server.core.ports import MemoryStore, ToolSpec
from server.core.state import SessionState
from server.core.tools import ToolRegistry

_TRACE_SOURCE = "core"
IDLE_TIMEOUT = 30.0
_MEMORY_HEADER = "[MÉMOIRE]"


class Coordinator:
    """Pure, deterministic event -> command transition function.

    Holds no direct I/O, no clock and imports no asyncio: it receives domain
    events and awaits only the async ports injected by the composition root
    (``MemoryStore`` today), then returns the commands to carry out.
    """

    def __init__(
        self,
        *,
        memory: MemoryStore | None = None,
        user_id: str = "owner",
        system_prompt: str = "",
        tools: ToolRegistry | None = None,
        recall_k: int = 5,
        recall_window_turns: int = 3,
        recall_max_distance: float = 0.6,
    ) -> None:
        self._memory = memory
        self._user_id = user_id
        self._system_prompt = system_prompt
        self._tools = tools  # Tool registry seam (issue #28).
        self._recall_k = recall_k
        self._recall_window_turns = recall_window_turns
        self._recall_max_distance = recall_max_distance
        self._state = SessionState.IDLE
        self._client_id = ""
        self._connection_id = ""
        self._session_id = ""
        self._visible = False
        self._auto = False
        self._session_counter = 0
        self._audio_buffer: list[bytes] = []
        self._recent_utterances: list[str] = []
        self._injected_memories: set[str] = set()

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @property
    def session_id(self) -> str:
        return self._session_id

    async def handle(self, event: Event) -> list[Command]:
        if isinstance(event, ClientRegistered):
            return self._on_client_registered(event)
        if isinstance(event, SessionRequested):
            return await self._on_session_requested(event)
        if isinstance(event, ClientAudio):
            return self._on_client_audio(event)
        if isinstance(event, ClientDisconnected):
            return self._on_client_disconnected(event)
        if isinstance(event, ClientInterrupt):
            return self._on_client_interrupt(event)
        if isinstance(event, ClientToolResult):
            return self._on_client_tool_result(event)
        if isinstance(event, LlmOpened):
            return self._on_llm_opened(event)
        if isinstance(event, LlmAudio):
            return self._on_llm_audio(event)
        if isinstance(event, LlmTranscription):
            return await self._on_llm_transcription(event)
        if isinstance(event, LlmToolCall):
            return await self._on_llm_tool_call(event)
        if isinstance(event, LlmClosed):
            return self._on_llm_closed(event)
        if isinstance(event, LlmFailed):
            return self._on_llm_failed(event)
        if isinstance(event, ShutdownRequested):
            return self._on_shutdown_requested(event)
        if isinstance(event, TimerFired):
            return self._on_timer_fired(event)
        raise TypeError(f"Unsupported event: {type(event).__name__}")

    def _on_client_registered(self, event: ClientRegistered) -> list[Command]:
        return self._decision("ClientRegistered", client_id=event.client_id)

    async def _on_session_requested(
        self, event: SessionRequested
    ) -> list[Command]:
        commands: list[Command] = []
        original_state = self._state
        if original_state is not SessionState.IDLE:
            commands.extend(self._teardown(SessionEndReason.REPLACED, "Replaced"))

        self._session_counter += 1
        self._connection_id = f"s{self._session_counter}"
        self._session_id = ""
        self._visible = False
        self._client_id = event.client_id
        self._auto = event.auto
        self._state = SessionState.LIVE_CONNECTING
        self._recent_utterances = []
        self._injected_memories = set()

        # Activation (D3): assemble the generic prompt and the memory context the
        # Session opens with. The instruction is frozen here, at connect time.
        # Pinned profile facts are part of that instruction, so seed the recall
        # dedup with them: recall must not re-inject what is already in context.
        context = ""
        if self._memory is not None:
            context = await self._memory.session_context(self._user_id)
            self._injected_memories = set(
                await self._memory.pinned_facts(self._user_id)
            )
        instruction = (self._system_prompt + "\n\n" + context).strip()

        commands.append(
            OpenLiveConnection(
                connection_id=self._connection_id,
                instruction=instruction,
                tools=self._open_tools(),
            )
        )
        commands.append(
            StartIdleTimer(connection_id=self._connection_id, timeout=IDLE_TIMEOUT)
        )
        commands.append(
            self._trace(
                "SessionRequested",
                from_state=original_state,
                to_state=self._state,
                client_id=event.client_id,
            )
        )
        commands.append(
            Trace(
                source=_TRACE_SOURCE,
                kind="Activation",
                payload={
                    "connection_id": self._connection_id,
                    "user_id": self._user_id,
                    "has_context": bool(context),
                    "context_chars": len(context),
                },
            )
        )
        return commands

    def _open_tools(self) -> tuple[ToolSpec, ...]:
        if self._tools is None:
            return ()
        return self._tools.specs()

    def _on_client_audio(self, event: ClientAudio) -> list[Command]:
        if self._state in (SessionState.LIVE_ACTIVE, SessionState.SESSION_VISIBLE):
            # Raw mic bytes are not voice activity: never re-arm the idle timer.
            return [
                SendAudioToLlm(connection_id=self._connection_id, data=event.data),
                *self._decision("ClientAudio"),
            ]
        return self._decision("ClientAudio")

    def _on_client_disconnected(self, event: ClientDisconnected) -> list[Command]:
        # La disparition du Client actif termine la Session par le chemin
        # déterministe unique ; celle d'un Client non-actif ne la touche pas.
        if (
            self._client_id
            and event.client_id == self._client_id
            and self._state is not SessionState.IDLE
        ):
            return self._teardown(SessionEndReason.CLIENT_GONE, "ClientDisconnected")
        return self._decision("ClientDisconnected", client_id=event.client_id)

    def _on_client_interrupt(self, event: ClientInterrupt) -> list[Command]:
        client_id = event.client_id or self._client_id
        if self._state is SessionState.SESSION_VISIBLE:
            # Barge-in: the client flushes its own playback; the Session stays
            # visible. No command is emitted for the desktop to stop audio.
            return self._decision("barge_in", client_id=client_id)
        return self._decision("ClientInterrupt", client_id=client_id)

    def _on_client_tool_result(self, event: ClientToolResult) -> list[Command]:
        # Reserved seam: T1 has no Tool routing, so the result is trace-only.
        return self._decision("ClientToolResult")

    def _on_llm_opened(self, event: LlmOpened) -> list[Command]:
        from_state = self._state
        if self._state is SessionState.LIVE_CONNECTING:
            self._state = SessionState.LIVE_ACTIVE
        return [
            self._trace(
                "LlmOpened",
                from_state=from_state,
                to_state=self._state,
                client_id=self._client_id,
            )
        ]

    def _on_llm_audio(self, event: LlmAudio) -> list[Command]:
        if self._state is SessionState.LIVE_ACTIVE:
            self._audio_buffer.append(event.data)
            return [
                self._rearm_timer(),
                *self._become_visible("LlmAudio"),
            ]
        if self._state is SessionState.SESSION_VISIBLE:
            return [
                self._rearm_timer(),
                SendAudioToClient(client_id=self._client_id, data=event.data),
                *self._decision("LlmAudio"),
            ]
        return self._decision("LlmAudio")

    async def _on_llm_transcription(
        self, event: LlmTranscription
    ) -> list[Command]:
        if event.role is Role.USER:
            return await self._on_user_utterance(event)
        if self._state is not SessionState.IDLE:
            await self._journal("assistant", event.text)
        if event.role is Role.ASSISTANT and self._state is SessionState.LIVE_ACTIVE:
            return self._become_visible("LlmTranscription")
        return self._decision("LlmTranscription")

    async def _on_user_utterance(self, event: LlmTranscription) -> list[Command]:
        if self._state is SessionState.IDLE:
            return self._decision("LlmTranscription")
        commands: list[Command] = [self._rearm_timer()]
        if self._memory is not None:
            await self._journal("user", event.text)
            text, trace = await self._recall(event.text)
            commands.append(trace)
            if text is not None:
                commands.append(
                    SendMemoryToLlm(connection_id=self._connection_id, text=text)
                )
        commands.extend(self._decision("LlmTranscription"))
        self._recent_utterances.append(event.text)
        return commands

    async def _journal(self, role: str, text: str) -> None:
        if self._memory is not None:
            await self._memory.append_turn(self._user_id, role, text)

    async def _recall(self, utterance: str) -> tuple[str | None, Trace]:
        window = self._recent_utterances[-self._recall_window_turns :]
        query = " ".join([*window, utterance])
        hits = await self._memory.recall(self._user_id, query)

        lines: list[str] = []
        for hit in hits:
            text = hit.text
            if not text or text in self._injected_memories:
                continue
            if hit.distance > self._recall_max_distance:
                continue
            self._injected_memories.add(text)
            lines.append(f"- {text}")
            if len(lines) >= self._recall_k:
                break

        trace = Trace(
            source=_TRACE_SOURCE,
            kind="Rappel",
            payload={
                "connection_id": self._connection_id,
                "query": query,
                "candidates": len(hits),
                "injected": len(lines),
            },
        )
        if not lines:
            return None, trace
        return _MEMORY_HEADER + "\n" + "\n".join(lines), trace

    async def _on_llm_tool_call(self, event: LlmToolCall) -> list[Command]:
        """Exécute un Tool du noyau et renvoie son résultat au modèle.

        Un nom inconnu (ou l'absence de registre) produit un résultat d'erreur
        tracé : le round-trip ne lève jamais, la Session reste vivante.
        """
        commands: list[Command] = [
            self._trace(
                "ToolCall",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
                level="debug",
                extra={"tool_id": event.tool_id, "name": event.name},
            )
        ]
        if self._tools is None:
            result: Mapping[str, object] = {
                "status": "error",
                "message": f"unknown tool: {event.name}",
            }
        else:
            result = await self._tools.execute(event.name, event.arguments)
        commands.append(
            SendToolResultToLlm(
                connection_id=self._connection_id,
                tool_id=event.tool_id,
                name=event.name,
                result=result,
            )
        )
        commands.append(
            self._trace(
                "ToolResult",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
                level="debug",
                extra={
                    "tool_id": event.tool_id,
                    "name": event.name,
                    "status": str(result.get("status", "ok")),
                },
            )
        )
        return commands

    def _on_llm_closed(self, event: LlmClosed) -> list[Command]:
        if not self._owns(event.connection_id):
            return self._decision(
                "LlmClosed", stale_connection_id=event.connection_id
            )
        if self._state in (
            SessionState.LIVE_CONNECTING,
            SessionState.LIVE_ACTIVE,
            SessionState.SESSION_VISIBLE,
        ):
            return self._teardown(SessionEndReason.NORMAL, "LlmClosed")
        return self._decision("LlmClosed")

    def _on_llm_failed(self, event: LlmFailed) -> list[Command]:
        if not self._owns(event.connection_id):
            return self._decision(
                "LlmFailed",
                cause=event.cause.value,
                stale_connection_id=event.connection_id,
            )
        if self._state is not SessionState.IDLE:
            return self._teardown(
                SessionEndReason.ERROR, "LlmFailed", cause=event.cause.value
            )
        return self._decision("LlmFailed", cause=event.cause.value)

    def _on_shutdown_requested(self, event: ShutdownRequested) -> list[Command]:
        if self._state is not SessionState.IDLE:
            return self._teardown(SessionEndReason.SHUTDOWN, "Shutdown")
        return self._decision("ShutdownRequested")

    def _on_timer_fired(self, event: TimerFired) -> list[Command]:
        if self._owns(event.connection_id) and self._state is not SessionState.IDLE:
            return self._teardown(SessionEndReason.TIMEOUT, "Timeout")
        return self._decision("TimerFired")

    def _rearm_timer(self) -> StartIdleTimer:
        return StartIdleTimer(connection_id=self._connection_id, timeout=IDLE_TIMEOUT)

    def _owns(self, connection_id: str) -> bool:
        """Une identité vide n'est pas un joker : seul l'id courant compte."""
        return connection_id != "" and connection_id == self._connection_id

    def _become_visible(self, kind: str) -> list[Command]:
        from_state = self._state
        self._state = SessionState.SESSION_VISIBLE
        self._visible = True
        self._session_id = self._connection_id
        commands: list[Command] = [
            AnnounceSessionStarted(client_id=self._client_id, auto=self._auto)
        ]
        for buffered in self._audio_buffer:
            commands.append(
                SendAudioToClient(client_id=self._client_id, data=buffered)
            )
        self._audio_buffer.clear()
        commands.append(
            self._trace(
                kind,
                from_state=from_state,
                to_state=self._state,
                client_id=self._client_id,
            )
        )
        return commands

    def _teardown(
        self,
        reason: SessionEndReason,
        kind: str,
        *,
        cause: str | None = None,
    ) -> list[Command]:
        """Single deterministic termination path.

        Ordering is fixed: cancel the idle timer, close the Live connection,
        announce the Session end only when the Session had become visible,
        then trace both transitions Idle <- Closing <- origin.
        Afterwards the Coordinator is back to Idle with no residual identity,
        so no zombie Session can linger.
        """
        from_state = self._state
        old_connection_id = self._connection_id
        old_session_id = self._session_id
        old_client_id = self._client_id
        was_visible = self._visible
        self._state = SessionState.CLOSING
        commands: list[Command] = [
            CancelIdleTimer(connection_id=old_connection_id),
            CloseLiveConnection(connection_id=old_connection_id),
        ]
        if was_visible:
            commands.append(
                AnnounceSessionEnded(client_id=old_client_id, reason=reason)
            )
        commands.append(
            self._trace(
                kind,
                from_state=from_state,
                to_state=SessionState.CLOSING,
                client_id=old_client_id,
                reason=reason.name,
                cause=cause,
                connection_id=old_connection_id,
                session_id=old_session_id,
            )
        )
        commands.append(
            self._trace(
                "SessionClosed",
                from_state=SessionState.CLOSING,
                to_state=SessionState.IDLE,
                client_id=old_client_id,
                reason=reason.name,
                cause=cause,
                connection_id=old_connection_id,
                session_id=old_session_id,
            )
        )
        self._state = SessionState.IDLE
        self._reset()
        return commands

    def _reset(self) -> None:
        self._client_id = ""
        self._connection_id = ""
        self._session_id = ""
        self._visible = False
        self._auto = False
        self._audio_buffer.clear()
        self._recent_utterances = []
        self._injected_memories = set()

    def _decision(
        self,
        kind: str,
        *,
        client_id: str | None = None,
        cause: str | None = None,
        stale_connection_id: str | None = None,
    ) -> list[Command]:
        """Trace for a decision that leaves the state unchanged."""
        return [
            self._trace(
                kind,
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id if client_id is None else client_id,
                cause=cause,
                level="debug",
                stale_connection_id=stale_connection_id,
            )
        ]

    def _trace(
        self,
        kind: str,
        *,
        from_state: SessionState,
        to_state: SessionState,
        client_id: str = "",
        reason: str | None = None,
        cause: str | None = None,
        connection_id: str | None = None,
        session_id: str | None = None,
        stale_connection_id: str | None = None,
        level: str = "info",
        extra: Mapping[str, object] | None = None,
    ) -> Trace:
        payload: dict[str, object] = {
            "connection_id": (
                self._connection_id if connection_id is None else connection_id
            ),
            "session_id": self._session_id if session_id is None else session_id,
            "client_id": client_id,
            "from_state": from_state.value,
            "to_state": to_state.value,
        }
        if reason is not None:
            payload["reason"] = reason
        if cause is not None:
            payload["cause"] = cause
        if extra is not None:
            payload.update(extra)
        if stale_connection_id is not None:
            payload["stale_connection_id"] = stale_connection_id
        return Trace(source=_TRACE_SOURCE, kind=kind, payload=payload, level=level)
