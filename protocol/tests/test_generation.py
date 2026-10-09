import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import google.protobuf
import pytest

PROTOCOL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PROTOCOL_DIR.parent
PROTO_FILE = PROTOCOL_DIR / "proto" / "messages.proto"
GENERATOR = PROTOCOL_DIR / "proto" / "generate.py"
GENERATED_DIR = PROTOCOL_DIR / "generated"
GENERATED_FILE = GENERATED_DIR / "messages_pb2.py"

# Frozen wire — source of truth: notes.md "Frozen wire" section.
FROZEN_MESSAGES = {
    "Register": {"client_tools": 1, "client_name": 2, "platform": 3},
    "Audio": {"data": 1},
    "SessionStart": {},
    "SessionEnd": {},
    "ToolResult": {"id": 1, "name": 2, "result_json": 3},
    "Ping": {},
    "Interrupt": {},
    "Registered": {"client_id": 1, "server_tools": 2},
    "SessionStarted": {"auto": 1},
    "SessionEnded": {"reason": 1},
    "ToolCall": {"id": 1, "name": 2, "args_json": 3},
    "Text": {"role": 1, "content": 2},
    "Notification": {"source": 1, "message": 2},
    "Pong": {},
    "Error": {"message": 1},
    "ToolDeclaration": {"name": 1, "description": 2, "parameters_json": 3},
    "Envelope": {
        "register": 1,
        "audio": 2,
        "session_start": 3,
        "session_end": 4,
        "tool_result": 5,
        "ping": 6,
        "interrupt": 7,
        "registered": 8,
        "session_started": 9,
        "session_ended": 10,
        "tool_call": 11,
        "text": 12,
        "notification": 13,
        "pong": 14,
        "error": 15,
    },
}

FROZEN_REASONS = {
    "NORMAL": 0,
    "TIMEOUT": 1,
    "SILENT_EVENT": 2,
    "REPLACED": 3,
    "SHUTDOWN": 4,
    "ERROR": 5,
}


def _require_generated():
    if not GENERATED_FILE.is_file():
        pytest.skip("generated code absent — see test_generated_module_exists")


def _load_generated():
    spec = importlib.util.spec_from_file_location(
        "_generated_messages_pb2", GENERATED_FILE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_module_exists():
    assert GENERATED_FILE.is_file(), (
        f"missing {GENERATED_FILE}; run `make generate`"
    )


def test_regeneration_is_byte_identical(tmp_path):
    _require_generated()
    subprocess.run(
        [sys.executable, str(GENERATOR), "--out", str(tmp_path)],
        check=True,
        cwd=str(REPO_ROOT),
    )
    assert (tmp_path / "messages_pb2.py").read_bytes() == GENERATED_FILE.read_bytes()
    assert (tmp_path / "__init__.py").read_bytes() == (
        GENERATED_DIR / "__init__.py"
    ).read_bytes()


def test_generated_file_is_not_tracked_by_git():
    result = subprocess.run(
        ["git", "ls-files", "protocol/generated"],
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "", (
        f"generated code must not be committed: {result.stdout!r}"
    )


def test_generated_header_version_matches_runtime():
    _require_generated()
    header = GENERATED_FILE.read_text(encoding="utf-8").splitlines()
    version_line = next(
        line for line in header if "Protobuf Python Version:" in line
    )
    declared = re.search(r"Protobuf Python Version:\s*(\S+)", version_line).group(1)
    assert declared == google.protobuf.__version__


def test_wire_is_frozen():
    _require_generated()
    descriptor = _load_generated().DESCRIPTOR

    assert descriptor.package == "athena"
    assert set(descriptor.message_types_by_name) == set(FROZEN_MESSAGES)

    for name, expected_fields in FROZEN_MESSAGES.items():
        message = descriptor.message_types_by_name[name]
        actual = {field.name: field.number for field in message.fields}
        assert actual == expected_fields, f"wire drift in {name}"

    envelope = descriptor.message_types_by_name["Envelope"]
    assert [oneof.name for oneof in envelope.oneofs] == ["payload"]
    assert [field.number for field in envelope.oneofs[0].fields] == list(
        range(1, 16)
    )

    reason = descriptor.message_types_by_name["SessionEnded"].enum_types_by_name[
        "Reason"
    ]
    assert {value.name: value.number for value in reason.values} == FROZEN_REASONS
