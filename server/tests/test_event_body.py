from __future__ import annotations

import ast
from pathlib import Path

from conftest import RecordingTraceSink
from server.adapters import event_body
from server.adapters.event_body import (
    INVALID_CONTENT_TYPE,
    INVALID_JSON_BODY,
    MAX_CONTENT_CHARS,
    AcceptedEvent,
    Rejection,
    emit_event_received,
    not_serving,
    validate_event,
)


def test_valid_payload_yields_trimmed_event_with_its_own_source() -> None:
    outcome = validate_event(
        {"source": "matrix", "content": "  porte ouverte  "},
        default_source="webhook",
    )

    assert outcome == AcceptedEvent(source="matrix", content="porte ouverte")


def test_missing_source_falls_back_to_the_default_given_by_the_caller() -> None:
    outcome = validate_event(
        {"content": "porte ouverte"}, default_source="console"
    )

    assert outcome == AcceptedEvent(source="console", content="porte ouverte")


def test_non_object_body_is_rejected() -> None:
    outcome = validate_event(["not", "an", "object"], default_source="webhook")

    assert outcome == Rejection(400, "body must be a JSON object")


def test_non_string_fields_are_rejected() -> None:
    outcome = validate_event(
        {"source": "matrix", "content": 12}, default_source="webhook"
    )

    assert outcome == Rejection(400, "source and content must be strings")


def test_blank_fields_are_rejected() -> None:
    blank_source = validate_event(
        {"source": "   ", "content": "porte ouverte"}, default_source="webhook"
    )
    blank_content = validate_event(
        {"source": "matrix", "content": "  "}, default_source="webhook"
    )

    assert blank_source == Rejection(
        400, "source and content must be non-empty"
    )
    assert blank_content == Rejection(
        400, "source and content must be non-empty"
    )


def test_content_up_to_the_ceiling_is_accepted() -> None:
    outcome = validate_event(
        {"content": "x" * MAX_CONTENT_CHARS}, default_source="console"
    )

    assert outcome == AcceptedEvent(
        source="console", content="x" * MAX_CONTENT_CHARS
    )


def test_content_beyond_the_ceiling_is_rejected() -> None:
    outcome = validate_event(
        {"content": "x" * (MAX_CONTENT_CHARS + 1)}, default_source="console"
    )

    assert outcome == Rejection(
        413, f"content exceeds {MAX_CONTENT_CHARS} characters"
    )


def test_fixed_rejections_share_the_surfaces_policy() -> None:
    assert INVALID_CONTENT_TYPE == Rejection(
        415, "Content-Type must be application/json"
    )
    assert INVALID_JSON_BODY == Rejection(400, "invalid JSON body")


def test_not_serving_rejection_names_the_surface() -> None:
    assert not_serving("webhook") == Rejection(503, "webhook not serving")
    assert not_serving("console") == Rejection(503, "console not serving")


def test_the_event_received_trace_is_shared_across_surfaces() -> None:
    sink = RecordingTraceSink()

    emit_event_received(
        sink, "console", AcceptedEvent(source="maison", content="porte")
    )
    emit_event_received(
        sink, "webhook", AcceptedEvent(source="maison", content="porte")
    )

    assert [entry.source for entry in sink.entries] == ["console", "webhook"]
    assert {entry.kind for entry in sink.entries} == {"event_received"}
    assert sink.entries[0].payload == {"source": "maison", "chars": 5}


def test_validation_module_does_not_import_aiohttp() -> None:
    """Le module est partagé hors HTTP : il ne doit rien devoir à aiohttp."""
    path = Path(event_body.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)

    assert not [
        module
        for module in modules
        if module == "aiohttp" or module.startswith("aiohttp.")
    ]
