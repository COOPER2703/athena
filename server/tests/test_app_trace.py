from __future__ import annotations

import ast
import asyncio
import io
import logging
from pathlib import Path

import ast_guard
import pytest

from server.adapters.trace import emit
from server.app.trace import (
    ConsoleTraceSink,
    FanOutTraceSink,
    FileTraceSink,
    build_trace_sink,
    install_logging_bridge,
    render,
)
from server.core.commands import Trace
from server.core.coordinator import Coordinator
from server.core.events import (
    ClientRegistered,
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


def test_debug_decisions_gates_a_real_decision_entry_end_to_end() -> None:
    off_stream = io.StringIO()
    off_sink = build_trace_sink(debug_decisions=False, stream=off_stream)
    on_stream = io.StringIO()
    on_sink = build_trace_sink(debug_decisions=True, stream=on_stream)

    decision = next(
        command
        for command in asyncio.run(
            Coordinator().handle(ClientRegistered(client_id="c1"))
        )
        if isinstance(command, Trace)
    )
    for sink in (off_sink, on_sink):
        emit(
            sink,
            decision.source,
            decision.kind,
            payload=decision.payload,
            level=decision.level,
        )

    assert "ClientRegistered" not in off_stream.getvalue()
    assert "ClientRegistered" in on_stream.getvalue()


def test_file_sink_writes_every_entry_when_level_is_debug(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "server.log"
    sink = FileTraceSink(path, min_level="debug")
    sink.emit(_entry(kind="SessionRequested"))
    sink.emit(_entry(kind="ClientAudio", level="debug"))
    sink.close()

    content = path.read_text(encoding="utf-8")
    assert "SessionRequested" in content
    assert "ClientAudio" in content
    assert path.parent.is_dir()


def test_file_sink_respects_info_level(tmp_path: Path) -> None:
    path = tmp_path / "server.log"
    sink = FileTraceSink(path, min_level="info")
    sink.emit(_entry(kind="SessionRequested"))
    sink.emit(_entry(kind="ClientAudio", level="debug"))
    sink.close()

    content = path.read_text(encoding="utf-8")
    assert "SessionRequested" in content
    assert "ClientAudio" not in content


def test_file_sink_rotates_and_keeps_a_backup(tmp_path: Path) -> None:
    path = tmp_path / "server.log"
    first = render(_entry(kind="First")) + "\n"
    sink = FileTraceSink(path, max_bytes=len(first) + 1, backup_count=1)
    sink.emit(_entry(kind="First"))
    sink.emit(_entry(kind="Second"))
    sink.close()

    assert "Second" in path.read_text(encoding="utf-8")
    backup = tmp_path / "server.log.1"
    assert backup.exists()
    assert "First" in backup.read_text(encoding="utf-8")


def test_fan_out_sink_feeds_every_consumer(tmp_path: Path) -> None:
    stream = io.StringIO()
    path = tmp_path / "server.log"
    sink = FanOutTraceSink(ConsoleTraceSink(stream), FileTraceSink(path))
    sink.emit(_entry(kind="LlmOpened"))
    sink.close()

    assert "LlmOpened" in stream.getvalue()
    assert "LlmOpened" in path.read_text(encoding="utf-8")


def test_build_trace_sink_tees_console_and_file(tmp_path: Path) -> None:
    stream = io.StringIO()
    sink = build_trace_sink(stream=stream, log_dir=tmp_path)
    sink.emit(_entry(kind="LlmOpened"))
    sink.close()

    assert "LlmOpened" in stream.getvalue()
    assert "LlmOpened" in (tmp_path / "server.log").read_text(encoding="utf-8")


def test_build_trace_sink_filters_the_file_at_the_configured_level(
    tmp_path: Path,
) -> None:
    sink = build_trace_sink(log_dir=tmp_path, level="info")
    sink.emit(_entry(kind="SessionRequested"))
    sink.emit(_entry(kind="ClientAudio", level="debug"))
    sink.close()

    content = (tmp_path / "server.log").read_text(encoding="utf-8")
    assert "SessionRequested" in content
    assert "ClientAudio" not in content


def test_build_trace_sink_keeps_debug_when_decisions_are_enabled(
    tmp_path: Path,
) -> None:
    sink = build_trace_sink(log_dir=tmp_path, level="info", debug_decisions=True)
    sink.emit(_entry(kind="ClientAudio", level="debug"))
    sink.close()

    assert "ClientAudio" in (tmp_path / "server.log").read_text(encoding="utf-8")


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
        for command in asyncio.run(coordinator.handle(event)):
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
        "Activation",
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
