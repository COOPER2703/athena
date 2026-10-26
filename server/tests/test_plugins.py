from __future__ import annotations

import asyncio
from typing import Mapping

import pytest

from server.core.plugins import PluginBinding, PluginRegistry, ToolBinding
from server.core.ports import ToolSpec
from server.core.tools import ToolNameConflict


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"outil {name}",
        parameters={"type": "object", "properties": {}},
    )


def _handler(name: str):
    async def handle(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return {"tool": name, "arguments": dict(arguments)}

    return handle


def _binding(name: str, *tools: str, instruction: str = "") -> PluginBinding:
    return PluginBinding(
        name=name,
        tools=tuple(
            ToolBinding(spec=_spec(tool), handler=_handler(tool)) for tool in tools
        ),
        instruction=instruction,
    )


def test_registry_aggregates_every_binding_with_its_owner() -> None:
    registry = PluginRegistry()
    registry.add(_binding("alpha", "alpha__turn_on", "alpha__turn_off"))
    registry.add(_binding("beta", "beta__status"))

    assert {spec.name for spec in registry.tools.specs()} == {
        "alpha__turn_on",
        "alpha__turn_off",
        "beta__status",
    }
    assert registry.tools.owner_of("alpha__turn_on") == "alpha"
    assert registry.tools.owner_of("beta__status") == "beta"
    assert registry.tools.owner_of("ghost") is None


def test_registry_refuses_a_tool_name_claimed_by_two_plugins() -> None:
    registry = PluginRegistry()
    registry.add(_binding("alpha", "shared"))

    with pytest.raises(ToolNameConflict) as excinfo:
        registry.add(_binding("beta", "shared"))

    message = str(excinfo.value)
    assert "shared" in message
    assert "alpha" in message
    assert "beta" in message


def test_instruction_joins_plugin_instructions_by_plugin_name() -> None:
    registry = PluginRegistry()
    registry.add(_binding("zeta", "zeta__ping", instruction="Instruction zeta."))
    registry.add(_binding("alpha", "alpha__ping", instruction="Instruction alpha."))
    registry.add(_binding("beta", "beta__ping"))

    assert registry.instruction() == (
        "Instruction alpha.\n\nInstruction zeta."
    )


def test_instruction_is_empty_when_no_plugin_provides_one() -> None:
    registry = PluginRegistry()
    registry.add(_binding("alpha", "alpha__ping", instruction="   "))

    assert registry.instruction() == ""


def test_instruction_disappears_with_the_disabled_plugin() -> None:
    enabled = _binding("alpha", "alpha__ping", instruction="Instruction alpha.")
    registry = PluginRegistry()
    registry.add(enabled)
    assert registry.instruction() == "Instruction alpha."

    # Désactiver un plugin, c'est reconstruire le registre sans son binding :
    # ses Tools et son instruction disparaissent ensemble.
    rebuilt = PluginRegistry()
    assert rebuilt.tools.specs() == ()
    assert rebuilt.instruction() == ""


def test_a_failing_plugin_handler_becomes_an_error_result() -> None:
    async def explode(arguments: Mapping[str, object]) -> Mapping[str, object]:
        raise RuntimeError("la maison ne répond pas")

    registry = PluginRegistry()
    registry.add(
        PluginBinding(
            name="alpha",
            tools=(ToolBinding(spec=_spec("alpha__boom"), handler=explode),),
        )
    )

    result = asyncio.run(registry.tools.execute("alpha__boom", {}))

    assert result["status"] == "error"
    assert "alpha__boom" in str(result["message"])
    assert "la maison ne répond pas" in str(result["message"])


def test_kernel_tools_are_owned_by_core_by_default() -> None:
    registry = PluginRegistry()
    registry.tools.register(_spec("save_memory"), _handler("save_memory"))

    assert registry.tools.owner_of("save_memory") == "core"
