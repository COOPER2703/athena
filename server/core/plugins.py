from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from plugin_api import (
    KNOWN_PLATFORMS,
    SERVER_PLATFORM,
    ConfigField,
    PluginBinding,
    PluginContext,
    PluginManifest,
    PluginUnavailable,
    SecretField,
    ToolBinding,
)
from server.core.tools import ToolRegistry

# Le contrat public vit dans ``plugin_api`` (aucun import de ``server``) ; ce
# module le ré-exporte pour que les imports existants du noyau continuent de
# marcher. Seul ``PluginRegistry`` appartient au noyau.

__all__ = [
    "KNOWN_PLATFORMS",
    "PLUGIN_STATES",
    "SERVER_PLATFORM",
    "ConfigField",
    "PluginBinding",
    "PluginContext",
    "PluginManifest",
    "PluginRegistry",
    "PluginUnavailable",
    "SecretField",
    "ToolBinding",
]

# États de chargement observables d'un plugin (GLOSSARY, section Plugins) :
# activé, désactivé, dossier manquant, ou en échec de chargement.
PLUGIN_STATES = ("activated", "disabled", "missing", "failed")


class PluginRegistry:
    """Agrégat pur des plugins activés et de leurs Tool (ADR-0014).

    Le registre ne fait aucune I/O : il agrège les ``PluginBinding`` des
    plugins chargés, attribue chaque Tool à son plugin et refuse les collisions
    de noms (fatal au démarrage). Il compose aussi l'instruction d'usage des
    plugins, dans un ordre déterministe, pour que le prompt système reste
    reproductible.

    Il porte aussi l'**état de chargement** de chaque plugin, pour qu'il soit
    lisible sans relire la trace : c'est le catalogue décrit par le glossaire.
    Le chargeur l'entretient ; la trace reste émise par l'adaptateur.
    """

    def __init__(self) -> None:
        self._tools = ToolRegistry()
        self._instructions: dict[str, str] = {}
        self._states: dict[str, str] = {}

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    @property
    def states(self) -> Mapping[str, str]:
        """États connus des plugins, en lecture seule."""
        return MappingProxyType(self._states)

    def state_of(self, plugin: str) -> str | None:
        """État de chargement d'un plugin, ``None`` s'il n'a jamais été vu."""
        return self._states.get(plugin)

    def mark_state(self, plugin: str, state: str) -> None:
        """Enregistre l'état de chargement d'un plugin.

        Les états sont ceux du glossaire ; une valeur inconnue signale une
        dérive du chargeur, pas une donnée utilisateur.
        """
        if state not in PLUGIN_STATES:
            raise ValueError(
                f"unknown plugin state {state!r} (known: {', '.join(PLUGIN_STATES)})"
            )
        self._states[plugin.strip()] = state

    def add(self, binding: PluginBinding) -> None:
        """Enregistre tous les Tools d'un binding, attribués à son plugin.

        Un nom déjà pris lève ``ToolNameConflict`` : le message nomme les deux
        plugins, pour qu'un plugin n'écrase jamais silencieusement un autre.
        """
        name = binding.name.strip()
        if not name:
            raise ValueError("plugin name is required")
        for tool in binding.tools:
            self._tools.register(tool.spec, tool.handler, owner=name)
        instruction = binding.instruction.strip()
        if instruction:
            self._instructions[name] = instruction

    def instruction(self) -> str:
        """Instructions des plugins activés, jointes par une ligne vide.

        Ordre fixé par le nom du plugin (jamais par l'ordre d'arrivée) ; renvoie
        la chaîne vide quand aucun plugin n'apporte d'instruction.
        """
        return "\n\n".join(
            self._instructions[name] for name in sorted(self._instructions)
        )
