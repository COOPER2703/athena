from __future__ import annotations

import json
from typing import Mapping

from conftest import FakeJudge, close_window, drive, handle, opened
from server.core import (
    Coordinator,
    EventReceived,
    SessionRequested,
    ToolSpec,
    Trace,
)
from server.core.plugins import PluginBinding, PluginRegistry, ToolBinding

PLUGIN_INSTRUCTION = "Utilise hass__turn_on pour allumer la maison."


class FakeMemory:
    """Port mémoire minimal : ne sert que le contexte d'Activation."""

    def __init__(self, context: str = "") -> None:
        self._context = context

    async def session_context(self, user_id: str) -> str:
        return self._context

    async def pinned_facts(self, user_id: str) -> list[str]:
        return [self._context] if self._context else []


def _binding(name: str, tool: str, instruction: str) -> PluginBinding:
    async def handler(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return {"status": "ok"}

    return PluginBinding(
        name=name,
        tools=(
            ToolBinding(
                spec=ToolSpec(
                    name=tool,
                    description=f"outil {tool}",
                    parameters={"type": "object", "properties": {}},
                ),
                handler=handler,
            ),
        ),
        instruction=instruction,
    )


def _plugin_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.add(_binding("hass", "hass__turn_on", PLUGIN_INSTRUCTION))
    return registry


def _activation(commands) -> Trace:
    return next(
        command
        for command in commands
        if isinstance(command, Trace) and command.kind == "Activation"
    )


def test_ordinary_session_instruction_carries_the_plugin_instruction() -> None:
    coordinator = Coordinator(
        memory=FakeMemory("MÉMOIRE"),
        user_id="alice",
        system_prompt="SYSTÈME",
        plugins=_plugin_registry(),
    )

    commands = drive(coordinator, [SessionRequested(client_id="c1")])

    live = opened(commands)
    assert live.instruction == "SYSTÈME\n\n" + PLUGIN_INSTRUCTION + "\n\nMÉMOIRE"
    # Les Tools du plugin sont déclarés sur la même Connexion que son
    # instruction : l'un ne va jamais sans l'autre.
    assert [spec.name for spec in live.tools] == ["hass__turn_on"]


def test_announcement_instruction_carries_the_plugin_instruction_too() -> None:
    coordinator = Coordinator(
        memory=FakeMemory("MÉMOIRE"),
        user_id="alice",
        system_prompt="SYSTÈME",
        announce_prompt="ANNONCE",
        judge=FakeJudge(),
        plugins=_plugin_registry(),
    )
    handle(coordinator, EventReceived(source="matrix", content="porte ouverte"))

    commands = close_window(coordinator)

    live = opened(commands)
    assert live.instruction == (
        "SYSTÈME\n\n" + PLUGIN_INSTRUCTION + "\n\nANNONCE\n\nMÉMOIRE"
    )
    assert [spec.name for spec in live.tools] == ["hass__turn_on"]


def test_two_plugin_instructions_are_composed_in_a_deterministic_order() -> None:
    alpha_then_zeta = PluginRegistry()
    alpha_then_zeta.add(_binding("alpha", "alpha__ping", "Instruction alpha."))
    alpha_then_zeta.add(_binding("zeta", "zeta__ping", "Instruction zeta."))

    zeta_then_alpha = PluginRegistry()
    zeta_then_alpha.add(_binding("zeta", "zeta__ping", "Instruction zeta."))
    zeta_then_alpha.add(_binding("alpha", "alpha__ping", "Instruction alpha."))

    def instruction(registry: PluginRegistry) -> str:
        commands = drive(
            Coordinator(system_prompt="SYSTÈME", plugins=registry),
            [SessionRequested(client_id="c1")],
        )
        return opened(commands).instruction

    expected = "SYSTÈME\n\nInstruction alpha.\n\nInstruction zeta."
    assert instruction(alpha_then_zeta) == expected
    # L'ordre d'arrivée des bindings ne change jamais le prompt : seul le nom
    # du plugin compte.
    assert instruction(zeta_then_alpha) == expected


def test_a_plugin_without_instruction_still_serves_its_tools() -> None:
    registry = PluginRegistry()
    registry.add(_binding("hass", "hass__turn_on", ""))

    commands = drive(
        Coordinator(system_prompt="SYSTÈME", plugins=registry),
        [SessionRequested(client_id="c1")],
    )

    live = opened(commands)
    assert live.instruction == "SYSTÈME"
    assert [spec.name for spec in live.tools] == ["hass__turn_on"]


def test_disabled_plugin_removes_its_tools_and_its_instruction_together() -> None:
    enabled = Coordinator(system_prompt="SYSTÈME", plugins=_plugin_registry())
    disabled = Coordinator(system_prompt="SYSTÈME", plugins=PluginRegistry())

    enabled_live = opened(drive(enabled, [SessionRequested(client_id="c1")]))
    disabled_live = opened(drive(disabled, [SessionRequested(client_id="c1")]))

    assert PLUGIN_INSTRUCTION in enabled_live.instruction
    assert enabled_live.tools
    assert disabled_live.instruction == "SYSTÈME"
    assert disabled_live.tools == ()


def test_without_plugins_the_instruction_is_the_same_as_before() -> None:
    memory = FakeMemory("MÉMOIRE")
    before = opened(
        drive(
            Coordinator(memory=memory, user_id="alice", system_prompt="SYSTÈME"),
            [SessionRequested(client_id="c1")],
        )
    )
    empty_registry = opened(
        drive(
            Coordinator(
                memory=memory,
                user_id="alice",
                system_prompt="SYSTÈME",
                plugins=PluginRegistry(),
            ),
            [SessionRequested(client_id="c1")],
        )
    )

    assert before.instruction == "SYSTÈME\n\nMÉMOIRE"
    assert empty_registry.instruction == before.instruction
    assert empty_registry.tools == before.tools == ()


def test_activation_traces_the_plugin_instruction_size_not_its_text() -> None:
    commands = drive(
        Coordinator(
            system_prompt="SYSTÈME", plugins=_plugin_registry()
        ),
        [SessionRequested(client_id="c1")],
    )

    payload = _activation(commands).payload

    assert payload["has_plugin_instruction"] is True
    assert payload["plugin_instruction_chars"] == len(PLUGIN_INSTRUCTION)
    assert PLUGIN_INSTRUCTION not in json.dumps(payload)


def test_activation_reports_no_plugin_instruction_without_a_plugin() -> None:
    commands = drive(Coordinator(), [SessionRequested(client_id="c1")])

    payload = _activation(commands).payload

    assert payload["has_plugin_instruction"] is False
    assert payload["plugin_instruction_chars"] == 0
