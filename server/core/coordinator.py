from __future__ import annotations

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
    StartIdleTimer,
    Trace,
)
from server.core.events import (
    ClientAudio,
    ClientInterrupt,
    ClientRegistered,
    ClientToolResult,
    Event,
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmOpened,
    LlmTranscription,
    Role,
    SessionRequested,
    ShutdownRequested,
    TimerFired,
)
from server.core.state import SessionState

_TRACE_SOURCE = "core"
IDLE_TIMEOUT = 30.0


class Coordinator:
    """Pure, deterministic event -> command transition function.

    Holds no I/O, no clock, no asyncio: it receives domain events and returns
    the commands the composition root should carry out.
    """

    def __init__(self) -> None:
        self._state = SessionState.IDLE
        self._client_id = ""
        self._connection_id = ""
        self._session_id = ""
        self._visible = False
        self._auto = False
        self._session_counter = 0
        self._audio_buffer: list[bytes] = []

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def connection_id(self) -> str:
        return self._connection_id

    @property
    def session_id(self) -> str:
        return self._session_id

    def handle(self, event: Event) -> list[Command]:
        if isinstance(event, ClientRegistered):
            return self._on_client_registered(event)
        if isinstance(event, SessionRequested):
            return self._on_session_requested(event)
        if isinstance(event, ClientAudio):
            return self._on_client_audio(event)
        if isinstance(event, ClientInterrupt):
            return self._on_client_interrupt(event)
        if isinstance(event, ClientToolResult):
            return self._on_client_tool_result(event)
        if isinstance(event, LlmOpened):
            return self._on_llm_opened(event)
        if isinstance(event, LlmAudio):
            return self._on_llm_audio(event)
        if isinstance(event, LlmTranscription):
            return self._on_llm_transcription(event)
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

    def _on_session_requested(self, event: SessionRequested) -> list[Command]:
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
        commands.append(OpenLiveConnection(connection_id=self._connection_id))
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
        return commands

    def _on_client_audio(self, event: ClientAudio) -> list[Command]:
        if self._state in (SessionState.LIVE_ACTIVE, SessionState.SESSION_VISIBLE):
            # Raw mic bytes are not voice activity: never re-arm the idle timer.
            return [
                SendAudioToLlm(connection_id=self._connection_id, data=event.data),
                *self._decision("ClientAudio"),
            ]
        return self._decision("ClientAudio")

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

    def _on_llm_transcription(self, event: LlmTranscription) -> list[Command]:
        if event.role is Role.USER and self._state is not SessionState.IDLE:
            return [
                self._rearm_timer(),
                *self._decision("LlmTranscription"),
            ]
        if event.role is Role.ASSISTANT and self._state is SessionState.LIVE_ACTIVE:
            return self._become_visible("LlmTranscription")
        return self._decision("LlmTranscription")

    def _on_llm_closed(self, event: LlmClosed) -> list[Command]:
        if self._state in (
            SessionState.LIVE_CONNECTING,
            SessionState.LIVE_ACTIVE,
            SessionState.SESSION_VISIBLE,
        ):
            return self._teardown(SessionEndReason.NORMAL, "LlmClosed")
        return self._decision("LlmClosed")

    def _on_llm_failed(self, event: LlmFailed) -> list[Command]:
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
        if (
            self._connection_id
            and event.connection_id == self._connection_id
            and self._state is not SessionState.IDLE
        ):
            return self._teardown(SessionEndReason.TIMEOUT, "Timeout")
        return self._decision("TimerFired")

    def _rearm_timer(self) -> StartIdleTimer:
        return StartIdleTimer(connection_id=self._connection_id, timeout=IDLE_TIMEOUT)

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
        announce the Session end (unless a client that never saw a Session is
        closed cleanly), then trace both transitions Idle <- Closing <- origin.
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
        if was_visible or reason is not SessionEndReason.NORMAL:
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

    def _decision(
        self,
        kind: str,
        *,
        client_id: str | None = None,
        cause: str | None = None,
    ) -> list[Command]:
        """Trace for a decision that leaves the state unchanged."""
        return [
            self._trace(
                kind,
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id if client_id is None else client_id,
                cause=cause,
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
        return Trace(source=_TRACE_SOURCE, kind=kind, payload=payload)
