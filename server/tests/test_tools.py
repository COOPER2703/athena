from __future__ import annotations

import asyncio
from typing import Mapping

import pytest

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


async def _explode(arguments: Mapping[str, object]) -> Mapping[str, object]:
    raise RuntimeError("handler cassé")


def test_a_plugin_tool_failure_becomes_an_error_result() -> None:
    """Un handler de plugin est du code externe : sa panne ne tue pas la Session."""
    registry = ToolRegistry()
    registry.register(_echo_spec(), _explode, owner="fixture")

    result = asyncio.run(registry.execute("echo", {}))

    assert result["status"] == "error"
    assert "handler cassé" in str(result["message"])


def test_a_kernel_tool_failure_still_propagates() -> None:
    """Un bug du noyau ne se déguise pas en erreur de Tool : il remonte."""
    registry = ToolRegistry()
    registry.register(_echo_spec(), _explode)

    with pytest.raises(RuntimeError, match="handler cassé"):
        asyncio.run(registry.execute("echo", {}))
