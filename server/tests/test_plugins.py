from __future__ import annotations

import asyncio
from typing import Mapping

import pytest

from server.core.plugins import (
    ConfigField,
    PluginBinding,
    PluginDeclaration,
    PluginRegistry,
    SecretField,
    ToolBinding,
)
from server.core.ports import ToolSpec
from server.core.session_tools import END_SESSION, register_session_tools
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


def test_a_plugin_cannot_take_the_kernel_end_session_name() -> None:
    """Le Tool du noyau est enregistré d'abord : un plugin qui le revendique échoue."""
    registry = PluginRegistry()
    register_session_tools(registry.tools)

    with pytest.raises(ToolNameConflict) as excinfo:
        registry.add(_binding("rogue", END_SESSION))

    message = str(excinfo.value)
    assert END_SESSION in message
    assert "core" in message
    assert "rogue" in message


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


def test_instruction_disappears_when_the_registry_is_rebuilt_without_the_plugin() -> None:
    enabled = _binding("alpha", "alpha__ping", instruction="Instruction alpha.")
    registry = PluginRegistry()
    registry.add(enabled)
    assert registry.instruction() == "Instruction alpha."

    # Reconstruire le registre sans le binding d'un plugin (parce qu'il n'est
    # pas retenu) fait disparaître ses Tools et son instruction ensemble.
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


def test_registry_exposes_the_loading_state_of_each_plugin() -> None:
    """Le Plugin Registry est le catalogue des plugins et de leur état (glossaire)."""
    registry = PluginRegistry()

    assert registry.state_of("alpha") is None
    registry.mark_state("alpha", "activated")
    registry.mark_state("beta", "failed")

    assert registry.state_of("alpha") == "activated"
    assert registry.state_of("beta") == "failed"
    assert dict(registry.states) == {"alpha": "activated", "beta": "failed"}


def test_registry_refuses_an_unknown_plugin_state() -> None:
    registry = PluginRegistry()

    with pytest.raises(ValueError) as excinfo:
        registry.mark_state("alpha", "activatd")

    assert "activatd" in str(excinfo.value)


def test_registry_knows_the_six_service_states_and_no_disabled_one() -> None:
    """Les six États de service du glossaire ; ``disabled`` n'en fait plus partie."""
    registry = PluginRegistry()

    for state in (
        "activated",
        "failed",
        "missing",
        "inactive",
        "unsupported",
        "invalid",
    ):
        registry.mark_state(f"plugin-{state}", state)

    assert set(registry.states.values()) == {
        "activated",
        "failed",
        "missing",
        "inactive",
        "unsupported",
        "invalid",
    }
    with pytest.raises(ValueError):
        registry.mark_state("legacy", "disabled")


def test_registry_keeps_the_failure_reason_and_retry_beside_the_state() -> None:
    """La raison et la retente vivent à côté de l'état, lisibles sans relire la Trace."""
    registry = PluginRegistry()
    registry.mark_state(
        "websearch",
        "failed",
        reason="PluginUnavailable: Tavily ne répond pas",
        retry=True,
    )

    assert registry.state_of("websearch") == "failed"
    assert registry.reason_of("websearch") == "PluginUnavailable: Tavily ne répond pas"
    assert registry.retry_of("websearch") is True
    assert registry.reason_of("absent") is None
    assert registry.retry_of("absent") is False

    # Une transition qui n'échoue plus efface la raison et la retente : sinon
    # la fiche continuerait d'afficher une panne réparée.
    registry.mark_state("websearch", "activated")

    assert registry.reason_of("websearch") is None
    assert registry.retry_of("websearch") is False


def test_registry_exposes_the_instruction_of_a_loaded_plugin() -> None:
    """L'Instruction se lit par plugin ; un plugin non chargé n'en a pas."""
    registry = PluginRegistry()
    registry.add(_binding("websearch", "websearch__search", instruction="Cherche."))

    assert registry.instruction_of("websearch") == "Cherche."
    assert registry.instruction_of("notion") == ""
    assert registry.instruction() == "Cherche."


def test_registry_keeps_the_declaration_alongside_the_service_state() -> None:
    """Tout dossier installé est observable par sa Déclaration, même non chargé."""
    declaration = PluginDeclaration(
        description="Cherche sur le web via Tavily",
        version="1.0.0",
        api_version=1,
        platform="server",
        directory="plugins/websearch",
        config=(ConfigField(key="base_url", type="url", required=False),),
        secrets=(SecretField(key="api_key", required=True),),
    )
    registry = PluginRegistry()
    registry.declare("websearch", declaration)
    registry.mark_state("websearch", "inactive")

    assert registry.declaration_of("websearch") is declaration
    assert registry.declaration_of("absent") is None
    assert dict(registry.declarations) == {"websearch": declaration}
    assert registry.state_of("websearch") == "inactive"
