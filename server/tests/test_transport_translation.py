from __future__ import annotations

from protocol import (
    Audio,
    Interrupt,
    Ping,
    Register,
    SessionEnd,
    SessionEnded,
    SessionEndReason,
    SessionStart,
    SessionStarted,
    ToolResult,
)
from server.adapters.transport.translation import outbound_message, translate_inbound
from server.core.commands import (
    AnnounceSessionEnded,
    AnnounceSessionStarted,
    CancelIdleTimer,
    CloseLiveConnection,
    OpenLiveConnection,
    SendAudioToClient,
    SendAudioToLlm,
    StartIdleTimer,
)
from server.core.events import (
    ClientAudio,
    ClientInterrupt,
    ClientToolResult,
    SessionRequested,
)


def test_audio_maps_to_client_audio() -> None:
    assert translate_inbound("c1", Audio(data=b"pcm")) == ClientAudio(data=b"pcm")


def test_session_start_maps_to_session_requested() -> None:
    assert translate_inbound("c1", SessionStart()) == SessionRequested(client_id="c1")


def test_interrupt_maps_to_client_interrupt() -> None:
    assert translate_inbound("c1", Interrupt()) == ClientInterrupt(client_id="c1")


def test_register_is_not_an_inbound_event() -> None:
    assert translate_inbound("c1", Register(client_name="desktop")) is None


def test_ping_is_not_an_inbound_event() -> None:
    assert translate_inbound("c1", Ping()) is None


def test_tool_result_maps_to_client_tool_result() -> None:
    assert translate_inbound(
        "c1", ToolResult(id="t1", name="shell", result_json=b"{}")
    ) == ClientToolResult(tool_id="t1", name="shell", result_json=b"{}")


def test_session_end_has_no_core_event_in_t1() -> None:
    assert translate_inbound("c1", SessionEnd()) is None


def test_outbound_audio_targets_client() -> None:
    mapping = outbound_message(SendAudioToClient(client_id="c1", data=b"pcm"))
    assert mapping == ("c1", Audio(data=b"pcm"))


def test_outbound_session_started() -> None:
    mapping = outbound_message(AnnounceSessionStarted(client_id="c1", auto=True))
    assert mapping == ("c1", SessionStarted(auto=True))


def test_outbound_session_ended() -> None:
    mapping = outbound_message(
        AnnounceSessionEnded(client_id="c1", reason=SessionEndReason.TIMEOUT)
    )
    assert mapping == ("c1", SessionEnded(reason=SessionEndReason.TIMEOUT))


def test_non_transport_commands_have_no_outbound_message() -> None:
    assert outbound_message(OpenLiveConnection(connection_id="s1")) is None
    assert outbound_message(CloseLiveConnection(connection_id="s1")) is None
    assert outbound_message(SendAudioToLlm(connection_id="s1", data=b"x")) is None
    assert outbound_message(StartIdleTimer(connection_id="s1", timeout=1.0)) is None
    assert outbound_message(CancelIdleTimer(connection_id="s1")) is None
