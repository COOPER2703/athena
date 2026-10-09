from __future__ import annotations

import ast
import io
import logging
from pathlib import Path

import ast_guard
import pytest

from server.adapters.trace import emit
from server.app.trace import (
    ConsoleTraceSink,
    build_trace_sink,
    install_logging_bridge,
    render,
)
from server.core.commands import Trace
from server.core.coordinator import Coordinator
from server.core.events import (
    LlmOpened,
    LlmTranscription,
    Role,
    SessionRequested,
    ShutdownRequested,
)
from server.core.ports import TraceEntry

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _entry(
    *,
    source: str = "core",
    kind: str = "SessionRequested",
    payload: dict[str, object] | None = None,
    level: str = "info",
    timestamp: float = 1_700_000_000.0,
) -> TraceEntry:
    return TraceEntry(
        timestamp=timestamp,
        source=source,
        kind=kind,
        payload=payload or {},
        level=level,
    )


def test_render_exposes_every_structured_field() -> None:
    line = render(
        _entry(kind="SessionRequested", payload={"to_state": "live_active"})
    )
    assert "core" in line
    assert "SessionRequested" in line
    assert "to_state" in line
    assert "live_active" in line
    assert "INFO" in line


def test_console_sink_writes_rendered_entry_to_its_stream() -> None:
    stream = io.StringIO()
    sink = ConsoleTraceSink(stream)
    entry = _entry(kind="LlmOpened")
    sink.emit(entry)
    assert "LlmOpened" in stream.getvalue()
    assert sink.entries == [entry]


def test_default_verbosity_keeps_debug_entry_but_hides_its_render() -> None:
    stream = io.StringIO()
    sink = ConsoleTraceSink(stream)
    entry = _entry(kind="ClientAudio", level="debug")
    sink.emit(entry)
    assert sink.entries == [entry]
    assert stream.getvalue() == ""


def test_debug_decisions_raises_rendered_verbosity() -> None:
    stream = io.StringIO()
    sink = build_trace_sink(debug_decisions=True, stream=stream)
    sink.emit(_entry(kind="ClientAudio", level="debug"))
    assert "ClientAudio" in stream.getvalue()


def test_debug_decisions_off_by_default_hides_debug_render() -> None:
    stream = io.StringIO()
    sink = build_trace_sink(debug_decisions=False, stream=stream)
    sink.emit(_entry(kind="ClientAudio", level="debug"))
    assert stream.getvalue() == ""


def test_logging_bridge_feeds_third_party_logs_into_the_stream() -> None:
    stream = io.StringIO()
    sink = ConsoleTraceSink(stream)
    original_handlers = logging.getLogger().handlers[:]
    original_level = logging.getLogger().level
    try:
        install_logging_bridge(sink)
        logging.getLogger("third.party").warning("boom")
    finally:
        logging.getLogger().handlers[:] = original_handlers
        logging.getLogger().setLevel(original_level)

    entry = sink.entries[-1]
    assert entry.source == "logging"
    assert entry.kind == "third.party"
    assert entry.payload == {"message": "boom"}
    assert entry.level == "warning"
    assert "boom" in stream.getvalue()


def test_logging_bridge_leaves_no_handler_outside_the_stream() -> None:
    sink = ConsoleTraceSink(io.StringIO())
    original_handlers = logging.getLogger().handlers[:]
    original_level = logging.getLogger().level
    try:
        handler = install_logging_bridge(sink)
        assert logging.getLogger().handlers == [handler]
    finally:
        logging.getLogger().handlers[:] = original_handlers
        logging.getLogger().setLevel(original_level)


def test_session_transition_is_traced_in_order_with_core_source() -> None:
    sink = ConsoleTraceSink(io.StringIO())
    coordinator = Coordinator()
    events = [
        SessionRequested(client_id="c1"),
        LlmOpened(),
        LlmTranscription(text="hi", role=Role.ASSISTANT),
        ShutdownRequested(),
    ]
    for event in events:
        for command in coordinator.handle(event):
            if isinstance(command, Trace):
                emit(
                    sink,
                    command.source,
                    command.kind,
                    payload=command.payload,
                    level=command.level,
                )

    assert [entry.kind for entry in sink.entries] == [
        "SessionRequested",
        "LlmOpened",
        "LlmTranscription",
        "Shutdown",
        "SessionClosed",
    ]
    assert {entry.source for entry in sink.entries} == {"core"}


def test_config_and_secrets_modules_never_log_outside_the_stream() -> None:
    for path in sorted(APP_DIR.glob("*.py")):
        if path.name == "trace.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        assert "logging" not in ast_guard.imports(tree), (
            f"{path.name} logs outside the trace stream"
        )
