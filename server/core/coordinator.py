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
    Event,
    LlmAudio,
    LlmClosed,
    LlmFailed,
    LlmOpened,
    LlmTranscription,
    Role,
    SessionRequested,
    TimerFired,
)
from server.core.state import SessionState

_TRACE_SOURCE = "core"
_IDLE_TIMEOUT = 30.0


class Coordinator:
    """Pure, deterministic event -> command transition function.

    Holds no I/O, no clock, no asyncio: it receives domain events and returns
    the commands the composition root should carry out.
    """

    def __init__(self) -> None:
        self._state = SessionState.IDLE
        self._client_id = ""
        self._session_id = ""
        self._auto = False
        self._session_counter = 0
        self._audio_buffer: list[bytes] = []

    @property
    def state(self) -> SessionState:
        return self._state

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
        if isinstance(event, TimerFired):
            return self._on_timer_fired(event)
        raise TypeError(f"Unsupported event: {type(event).__name__}")

    def _on_client_registered(self, event: ClientRegistered) -> list[Command]:
        return [
            self._trace(
                "ClientRegistered",
                from_state=self._state,
                to_state=self._state,
                client_id=event.client_id,
            )
        ]

    def _on_session_requested(self, event: SessionRequested) -> list[Command]:
        commands: list[Command] = []
        if self._state is not SessionState.IDLE:
            commands.extend(
                self._teardown(SessionEndReason.REPLACED, "Replaced")
            )

        self._session_counter += 1
        self._session_id = f"s{self._session_counter}"
        self._client_id = event.client_id
        self._auto = event.auto
        from_state = self._state
        self._state = SessionState.LIVE_CONNECTING
        commands.append(OpenLiveConnection(session_id=self._session_id))
        commands.append(
            StartIdleTimer(session_id=self._session_id, timeout=_IDLE_TIMEOUT)
        )
        commands.append(
            self._trace(
                "SessionRequested",
                from_state=from_state,
                to_state=self._state,
                client_id=event.client_id,
            )
        )
        return commands

    def _on_client_audio(self, event: ClientAudio) -> list[Command]:
        if self._state in (SessionState.LIVE_ACTIVE, SessionState.SESSION_VISIBLE):
            # Raw mic bytes are not voice activity: never re-arm the idle timer.
            return [
                SendAudioToLlm(session_id=self._session_id, data=event.data),
                self._trace(
                    "ClientAudio",
                    from_state=self._state,
                    to_state=self._state,
                    client_id=self._client_id,
                ),
            ]
        return [
            self._trace(
                "ClientAudio",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
            )
        ]

    def _on_client_interrupt(self, event: ClientInterrupt) -> list[Command]:
        client_id = event.client_id or self._client_id
        if self._state is SessionState.SESSION_VISIBLE:
            # Barge-in: the client flushes its own playback; the Session stays
            # visible. No command is emitted for the desktop to stop audio.
            return [
                self._trace(
                    "barge_in",
                    from_state=self._state,
                    to_state=self._state,
                    client_id=client_id,
                )
            ]
        return [
            self._trace(
                "ClientInterrupt",
                from_state=self._state,
                to_state=self._state,
                client_id=client_id,
            )
        ]

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
                self._trace(
                    "LlmAudio",
                    from_state=self._state,
                    to_state=self._state,
                    client_id=self._client_id,
                ),
            ]
        return [
            self._trace(
                "LlmAudio",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
            )
        ]

    def _on_llm_transcription(self, event: LlmTranscription) -> list[Command]:
        if event.role is Role.USER and self._state is not SessionState.IDLE:
            return [
                self._rearm_timer(),
                self._trace(
                    "LlmTranscription",
                    from_state=self._state,
                    to_state=self._state,
                    client_id=self._client_id,
                ),
            ]
        if event.role is Role.ASSISTANT and self._state is SessionState.LIVE_ACTIVE:
            return self._become_visible("LlmTranscription")
        return [
            self._trace(
                "LlmTranscription",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
            )
        ]

    def _on_llm_closed(self, event: LlmClosed) -> list[Command]:
        if self._state in (
            SessionState.LIVE_CONNECTING,
            SessionState.LIVE_ACTIVE,
            SessionState.SESSION_VISIBLE,
        ):
            return self._teardown(SessionEndReason.NORMAL, "LlmClosed")
        return [
            self._trace(
                "LlmClosed",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
            )
        ]

    def _on_llm_failed(self, event: LlmFailed) -> list[Command]:
        if self._state is not SessionState.IDLE:
            return self._teardown(
                SessionEndReason.ERROR, "LlmFailed", cause=event.cause.value
            )
        return [
            self._trace(
                "LlmFailed",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
                cause=event.cause.value,
            )
        ]

    def _on_timer_fired(self, event: TimerFired) -> list[Command]:
        if (
            self._session_id
            and event.session_id == self._session_id
            and self._state is not SessionState.IDLE
        ):
            return self._teardown(SessionEndReason.TIMEOUT, "Timeout")
        return [
            self._trace(
                "TimerFired",
                from_state=self._state,
                to_state=self._state,
                client_id=self._client_id,
            )
        ]

    def _rearm_timer(self) -> StartIdleTimer:
        return StartIdleTimer(session_id=self._session_id, timeout=_IDLE_TIMEOUT)

    def _become_visible(self, kind: str) -> list[Command]:
        from_state = self._state
        self._state = SessionState.SESSION_VISIBLE
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
        announce the Session end, then trace. Afterwards the Coordinator is
        back to Idle with no residual identity, so no zombie Session can linger.
        """
        from_state = self._state
        old_session_id = self._session_id
        old_client_id = self._client_id
        self._state = SessionState.CLOSING
        commands: list[Command] = [
            CancelIdleTimer(session_id=old_session_id),
            CloseLiveConnection(session_id=old_session_id),
            AnnounceSessionEnded(client_id=old_client_id, reason=reason),
            self._trace(
                kind,
                from_state=from_state,
                to_state=SessionState.IDLE,
                client_id=old_client_id,
                reason=reason.name,
                cause=cause,
                session_id=old_session_id,
            ),
        ]
        self._state = SessionState.IDLE
        self._reset()
        return commands

    def _reset(self) -> None:
        self._client_id = ""
        self._session_id = ""
        self._auto = False
        self._audio_buffer.clear()

    def _trace(
        self,
        kind: str,
        *,
        from_state: SessionState,
        to_state: SessionState,
        client_id: str = "",
        reason: str | None = None,
        cause: str | None = None,
        session_id: str | None = None,
    ) -> Trace:
        payload: dict[str, object] = {
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
