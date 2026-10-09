from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Union

from protocol.generated import messages_pb2 as pb


class ProtocolError(Exception):
    pass


class SessionEndReason(IntEnum):
    NORMAL = 0
    TIMEOUT = 1
    SILENT_EVENT = 2
    REPLACED = 3
    SHUTDOWN = 4
    ERROR = 5

    @classmethod
    def _missing_(cls, value: object) -> "SessionEndReason | None":
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        member = int.__new__(cls, value)
        member._name_ = f"UNKNOWN_{value}"
        member._value_ = value
        return member

    @classmethod
    def from_value(cls, value: int) -> "SessionEndReason":
        return cls(value)


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    name: str = ""
    description: str = ""
    parameters_json: str = ""


@dataclass(frozen=True, slots=True)
class Register:
    client_tools: list[ToolDeclaration] = field(default_factory=list)
    client_name: str = ""
    platform: str = ""


@dataclass(frozen=True, slots=True)
class Audio:
    data: bytes = b""


@dataclass(frozen=True, slots=True)
class SessionStart:
    pass


@dataclass(frozen=True, slots=True)
class SessionEnd:
    pass


@dataclass(frozen=True, slots=True)
class ToolResult:
    id: str = ""
    name: str = ""
    result_json: bytes = b""


@dataclass(frozen=True, slots=True)
class Ping:
    pass


@dataclass(frozen=True, slots=True)
class Interrupt:
    pass


@dataclass(frozen=True, slots=True)
class Registered:
    client_id: str = ""
    server_tools: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SessionStarted:
    auto: bool = False


@dataclass(frozen=True, slots=True)
class SessionEnded:
    reason: SessionEndReason = SessionEndReason.NORMAL


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str = ""
    name: str = ""
    args_json: bytes = b""


@dataclass(frozen=True, slots=True)
class Text:
    role: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class Notification:
    source: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class Pong:
    pass


@dataclass(frozen=True, slots=True)
class Error:
    message: str = ""


ClientToServer = Union[
    Register,
    Audio,
    SessionStart,
    SessionEnd,
    ToolResult,
    Ping,
    Interrupt,
]

ServerToClient = Union[
    Registered,
    Audio,
    SessionStarted,
    SessionEnded,
    ToolCall,
    Text,
    Notification,
    Pong,
    Error,
]

Message = Union[ClientToServer, ServerToClient]


_ToFn = Callable[[object, pb.Envelope], None]
_FromFn = Callable[[object], object]
_REGISTRY: dict[type, tuple[str, _ToFn, _FromFn]] = {}


def _register(
    message_type: type, field_name: str, to_fn: _ToFn, from_fn: _FromFn
) -> None:
    _REGISTRY[message_type] = (field_name, to_fn, from_fn)


def _to_register(message: Register, envelope: pb.Envelope) -> None:
    for declaration in message.client_tools:
        target = envelope.register.client_tools.add()
        target.name = declaration.name
        target.description = declaration.description
        target.parameters_json = declaration.parameters_json
    envelope.register.client_name = message.client_name
    envelope.register.platform = message.platform


def _from_register(inner: object) -> Register:
    return Register(
        client_tools=[
            ToolDeclaration(
                name=declaration.name,
                description=declaration.description,
                parameters_json=declaration.parameters_json,
            )
            for declaration in inner.client_tools
        ],
        client_name=inner.client_name,
        platform=inner.platform,
    )


def _to_audio(message: Audio, envelope: pb.Envelope) -> None:
    envelope.audio.data = message.data


def _from_audio(inner: object) -> Audio:
    return Audio(data=bytes(inner.data))


def _to_empty(field_name: str) -> _ToFn:
    def to(message: object, envelope: pb.Envelope) -> None:
        getattr(envelope, field_name).CopyFrom(getattr(pb, type(message).__name__)())

    return to


def _from_empty(message_type: type) -> _FromFn:
    return lambda inner: message_type()


def _to_tool_result(message: ToolResult, envelope: pb.Envelope) -> None:
    envelope.tool_result.id = message.id
    envelope.tool_result.name = message.name
    envelope.tool_result.result_json = message.result_json


def _from_tool_result(inner: object) -> ToolResult:
    return ToolResult(
        id=inner.id, name=inner.name, result_json=bytes(inner.result_json)
    )


def _to_registered(message: Registered, envelope: pb.Envelope) -> None:
    envelope.registered.client_id = message.client_id
    envelope.registered.server_tools.extend(message.server_tools)


def _from_registered(inner: object) -> Registered:
    return Registered(
        client_id=inner.client_id, server_tools=list(inner.server_tools)
    )


def _to_session_started(message: SessionStarted, envelope: pb.Envelope) -> None:
    envelope.session_started.auto = message.auto


def _from_session_started(inner: object) -> SessionStarted:
    return SessionStarted(auto=inner.auto)


def _to_session_ended(message: SessionEnded, envelope: pb.Envelope) -> None:
    envelope.session_ended.reason = int(message.reason)


def _from_session_ended(inner: object) -> SessionEnded:
    return SessionEnded(reason=SessionEndReason.from_value(inner.reason))


def _to_tool_call(message: ToolCall, envelope: pb.Envelope) -> None:
    envelope.tool_call.id = message.id
    envelope.tool_call.name = message.name
    envelope.tool_call.args_json = message.args_json


def _from_tool_call(inner: object) -> ToolCall:
    return ToolCall(id=inner.id, name=inner.name, args_json=bytes(inner.args_json))


def _to_text(message: Text, envelope: pb.Envelope) -> None:
    envelope.text.role = message.role
    envelope.text.content = message.content


def _from_text(inner: object) -> Text:
    return Text(role=inner.role, content=inner.content)


def _to_notification(message: Notification, envelope: pb.Envelope) -> None:
    envelope.notification.source = message.source
    envelope.notification.message = message.message


def _from_notification(inner: object) -> Notification:
    return Notification(source=inner.source, message=inner.message)


def _to_error(message: Error, envelope: pb.Envelope) -> None:
    envelope.error.message = message.message


def _from_error(inner: object) -> Error:
    return Error(message=inner.message)


_register(Register, "register", _to_register, _from_register)
_register(Audio, "audio", _to_audio, _from_audio)
_register(
    SessionStart,
    "session_start",
    _to_empty("session_start"),
    _from_empty(SessionStart),
)
_register(
    SessionEnd, "session_end", _to_empty("session_end"), _from_empty(SessionEnd)
)
_register(ToolResult, "tool_result", _to_tool_result, _from_tool_result)
_register(Ping, "ping", _to_empty("ping"), _from_empty(Ping))
_register(
    Interrupt, "interrupt", _to_empty("interrupt"), _from_empty(Interrupt)
)
_register(Registered, "registered", _to_registered, _from_registered)
_register(
    SessionStarted,
    "session_started",
    _to_session_started,
    _from_session_started,
)
_register(SessionEnded, "session_ended", _to_session_ended, _from_session_ended)
_register(ToolCall, "tool_call", _to_tool_call, _from_tool_call)
_register(Text, "text", _to_text, _from_text)
_register(Notification, "notification", _to_notification, _from_notification)
_register(Pong, "pong", _to_empty("pong"), _from_empty(Pong))
_register(Error, "error", _to_error, _from_error)


def encode(message: object) -> bytes:
    entry = _REGISTRY.get(type(message))
    if entry is None:
        raise ProtocolError(f"Unknown message type: {type(message).__name__}")
    _, to_fn, _ = entry
    envelope = pb.Envelope()
    to_fn(message, envelope)
    return envelope.SerializeToString()


def decode(data: bytes) -> Message:
    try:
        envelope = pb.Envelope()
        envelope.ParseFromString(data)
    except Exception as exc:
        raise ProtocolError(f"Failed to decode protobuf: {exc}") from exc

    field_name = envelope.WhichOneof("payload")
    if field_name is None:
        raise ProtocolError("Envelope has no payload")

    for field, _, from_fn in _REGISTRY.values():
        if field == field_name:
            return from_fn(getattr(envelope, field_name))

    raise ProtocolError(f"Unknown payload type: {field_name}")
