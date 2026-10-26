from __future__ import annotations

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


class PluginRegistry:
    """Agrégat pur des plugins activés et de leurs Tool (ADR-0014).

    Le registre ne fait aucune I/O : il agrège les ``PluginBinding`` des
    plugins chargés, attribue chaque Tool à son plugin et refuse les collisions
    de noms (fatal au démarrage). Il compose aussi l'instruction d'usage des
    plugins, dans un ordre déterministe, pour que le prompt système reste
    reproductible.
    """

    def __init__(self) -> None:
        self._tools = ToolRegistry()
        self._instructions: dict[str, str] = {}

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

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
