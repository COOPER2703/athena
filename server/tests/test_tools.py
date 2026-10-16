from __future__ import annotations

import asyncio
from typing import Mapping

from server.core.ports import ToolSpec
from server.core.tools import ToolRegistry


def _echo_spec() -> ToolSpec:
    return ToolSpec(
        name="echo",
        description="renvoie son entrée",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
    )


async def _echo(arguments: Mapping[str, object]) -> Mapping[str, object]:
    return {"echo": arguments.get("value", "")}


def test_register_then_specs_exposes_the_declaration() -> None:
    registry = ToolRegistry()
    registry.register(_echo_spec(), _echo)

    assert registry.specs() == (_echo_spec(),)


def test_execute_runs_the_registered_handler() -> None:
    registry = ToolRegistry()
    registry.register(_echo_spec(), _echo)

    result = asyncio.run(registry.execute("echo", {"value": "bonjour"}))

    assert result == {"echo": "bonjour"}


def test_unknown_tool_yields_a_non_fatal_error_result() -> None:
    registry = ToolRegistry()

    result = asyncio.run(registry.execute("ghost", {}))

    assert result == {"status": "error", "message": "unknown tool: ghost"}
