import pytest

from protocol import (
    Audio,
    ClientToServer,
    Error,
    Interrupt,
    Message,
    Notification,
    Ping,
    Pong,
    ProtocolError,
    Register,
    Registered,
    ServerToClient,
    SessionEnd,
    SessionEnded,
    SessionEndReason,
    SessionStart,
    SessionStarted,
    Text,
    ToolCall,
    ToolDeclaration,
    ToolResult,
    decode,
    encode,
)

MESSAGE_SAMPLES = {
    "register": Register(
        client_tools=[
            ToolDeclaration(
                name="run", description="Run a shell command", parameters_json="{}"
            ),
            ToolDeclaration(
                name="open", description="Open a url", parameters_json='{"url": "string"}'
            ),
        ],
        client_name="desktop",
        platform="linux",
    ),
    "audio": Audio(data=b"\x00\x01\x02\xff\xfe"),
    "session_start": SessionStart(),
    "session_end": SessionEnd(),
    "tool_result": ToolResult(id="t1", name="run", result_json=b'{"ok": true}'),
    "ping": Ping(),
    "interrupt": Interrupt(),
    "registered": Registered(client_id="c1", server_tools=["memorize", "search"]),
    "session_started": SessionStarted(auto=True),
    "session_ended": SessionEnded(reason=SessionEndReason.TIMEOUT),
    "tool_call": ToolCall(id="t2", name="open", args_json=b'{"url": "https://a.b"}'),
    "text": Text(role="assistant", content="hello"),
    "notification": Notification(source="matrix", message="new message"),
    "pong": Pong(),
    "error": Error(message="boom"),
}


def test_empty_message_round_trips():
    assert decode(encode(Ping())) == Ping()


@pytest.mark.parametrize("message", MESSAGE_SAMPLES.values(), ids=MESSAGE_SAMPLES.keys())
def test_every_message_round_trips(message):
    assert decode(encode(message)) == message


@pytest.mark.parametrize("message", MESSAGE_SAMPLES.values(), ids=MESSAGE_SAMPLES.keys())
def test_every_message_round_trips_at_its_defaults(message):
    default = type(message)()
    round_tripped = decode(encode(default))
    assert round_tripped == default
    assert type(round_tripped) is type(default)


@pytest.mark.parametrize("reason", list(SessionEndReason))
def test_session_ended_round_trips_every_known_reason(reason):
    assert decode(encode(SessionEnded(reason=reason))) == SessionEnded(reason=reason)


def test_session_ended_defaults_to_normal():
    assert decode(encode(SessionEnded())) == SessionEnded(reason=SessionEndReason.NORMAL)


def test_unknown_reason_is_preserved_not_coerced_to_normal():
    decoded = decode(encode(SessionEnded(reason=SessionEndReason(99))))
    assert int(decoded.reason) == 99
    assert decoded.reason.name == "UNKNOWN_99"
    assert decoded.reason != SessionEndReason.NORMAL


def test_audio_round_trips_raw_bytes():
    payload = bytes(range(256))
    assert decode(encode(Audio(data=payload))) == Audio(data=payload)


def test_unions_cover_the_wire():
    assert set(ClientToServer.__args__) == {
        Register,
        Audio,
        SessionStart,
        SessionEnd,
        ToolResult,
        Ping,
        Interrupt,
    }
    assert set(ServerToClient.__args__) == {
        Registered,
        Audio,
        SessionStarted,
        SessionEnded,
        ToolCall,
        Text,
        Notification,
        Pong,
        Error,
    }
    assert set(Message.__args__) == set(ClientToServer.__args__) | set(
        ServerToClient.__args__
    )


def test_garbage_bytes_raise_protocol_error():
    with pytest.raises(ProtocolError):
        decode(b"\xff\xff\xff\xff\xff")


def test_empty_bytes_raise_protocol_error():
    with pytest.raises(ProtocolError):
        decode(b"")


def test_envelope_without_payload_raises_protocol_error():
    from protocol.generated import messages_pb2 as pb

    with pytest.raises(ProtocolError):
        decode(pb.Envelope().SerializeToString())
