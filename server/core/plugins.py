from __future__ import annotations

from dataclasses import dataclass
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
    ToolSpec,
)
from server.core.tools import ToolRegistry

# Le contrat public vit dans ``plugin_api`` (aucun import de ``server``) ; ce
# module le ré-exporte pour que les imports existants du noyau continuent de
# marcher. Seuls ``PluginDeclaration`` et ``PluginRegistry`` appartiennent au
# noyau.

__all__ = [
    "KNOWN_PLATFORMS",
    "PLUGIN_STATES",
    "SERVER_PLATFORM",
    "ConfigField",
    "PluginBinding",
    "PluginContext",
    "PluginDeclaration",
    "PluginManifest",
    "PluginRegistry",
    "PluginUnavailable",
    "SecretField",
    "ToolBinding",
    "ToolSpec",
]

# États de service observables d'un plugin installé (GLOSSARY, section
# Plugins) : activé, en échec, dossier manquant, inactif, non supporté, ou
# manifeste invalide. ``disabled`` confondait trois situations distinctes.
PLUGIN_STATES = (
    "activated",
    "failed",
    "missing",
    "inactive",
    "unsupported",
    "invalid",
)


@dataclass(frozen=True, slots=True)
class PluginDeclaration:
    """Ce qu'un manifeste annonce, lu sans importer ni exécuter le plugin.

    Identité, configuration attendue et noms des secrets requis : c'est la
    matière que le registre porte pour **tous** les dossiers installés, y
    compris ceux qui ne tourneront jamais. ``invalid_reason`` est renseignée
    quand le manifeste ne tient pas debout ; l'identité vaut alors ce qui a pu
    être lu. Aucune valeur de secret n'entre ici, seulement des clés.
    """

    description: str = ""
    version: str = ""
    api_version: int = 0
    platform: str = ""
    directory: str = ""
    config: tuple[ConfigField, ...] = ()
    secrets: tuple[SecretField, ...] = ()
    invalid_reason: str | None = None


class PluginRegistry:
    """Agrégat pur des plugins activés et de leurs Tool (ADR-0014).

    Le registre ne fait aucune I/O : il agrège les ``PluginBinding`` des
    plugins chargés, attribue chaque Tool à son plugin et refuse les collisions
    de noms (fatal au démarrage). Il compose aussi l'instruction d'usage des
    plugins, dans un ordre déterministe, pour que le prompt système reste
    reproductible.

    Il porte aussi l'**état de service** et la **Déclaration** de chaque plugin
    installé, pour qu'ils soient lisibles sans relire la trace : c'est le
    Plugin Registry décrit par le glossaire. Le chargeur l'entretient au
    démarrage ; l'instantané ne fait ensuite que lire le cache.
    """

    def __init__(self) -> None:
        self._tools = ToolRegistry()
        self._instructions: dict[str, str] = {}
        self._states: dict[str, str] = {}
        self._declarations: dict[str, PluginDeclaration] = {}

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    @property
    def states(self) -> Mapping[str, str]:
        """États connus des plugins, en lecture seule."""
        return MappingProxyType(self._states)

    @property
    def declarations(self) -> Mapping[str, PluginDeclaration]:
        """Déclarations connues des plugins installés, en lecture seule."""
        return MappingProxyType(self._declarations)

    def state_of(self, plugin: str) -> str | None:
        """État de service d'un plugin, ``None`` s'il n'a jamais été vu."""
        return self._states.get(plugin)

    def declaration_of(self, plugin: str) -> PluginDeclaration | None:
        """Déclaration d'un plugin installé, ``None`` si aucun manifeste lu."""
        return self._declarations.get(plugin)

    def declare(self, plugin: str, declaration: PluginDeclaration) -> None:
        """Enregistre la Déclaration lue d'un dossier installé."""
        name = plugin.strip()
        if not name:
            raise ValueError("plugin name is required")
        self._declarations[name] = declaration

    def mark_state(self, plugin: str, state: str) -> None:
        """Enregistre l'état de service d'un plugin.

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
