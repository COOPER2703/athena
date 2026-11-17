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
# marcher. Seul ``PluginRegistry`` appartient au noyau.

__all__ = [
    "KNOWN_PLATFORMS",
    "PLUGIN_STATES",
    "SERVER_PLATFORM",
    "ConfigField",
    "PluginBinding",
    "PluginContext",
    "PluginDescription",
    "PluginManifest",
    "PluginRegistry",
    "PluginUnavailable",
    "SecretField",
    "ToolBinding",
    "ToolSpec",
]

# États de chargement observables d'un plugin (GLOSSARY, section Plugins) :
# activé, désactivé, dossier manquant, ou en échec de chargement.
PLUGIN_STATES = ("activated", "disabled", "missing", "failed")


@dataclass(frozen=True, slots=True)
class PluginDescription:
    """Métadonnées d'un plugin, telles que la Console les affiche.

    Elle ne porte **jamais** de valeur de Secret : seulement les noms de clés
    déclarés par le manifeste. ``error`` dit pourquoi un manifeste est illisible
    ou pourquoi le chargement a échoué ; la raison est déjà expurgée par le
    chargeur. Une description existe même quand le plugin n'a pas été chargé,
    pour qu'un dossier cassé reste lisible sans faire échouer le démarrage.
    """

    name: str
    version: str = ""
    platform: str = ""
    description: str = ""
    config: tuple[ConfigField, ...] = ()
    secrets: tuple[SecretField, ...] = ()
    error: str = ""

    @classmethod
    def from_manifest(
        cls, manifest: PluginManifest, *, error: str = ""
    ) -> PluginDescription:
        """Décrit un manifeste lu ; ``error`` documente un chargement en échec."""
        return cls(
            name=manifest.name,
            version=manifest.version,
            platform=manifest.platform,
            description=manifest.description,
            config=manifest.config,
            secrets=manifest.secrets,
            error=error,
        )


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
        self._descriptions: dict[str, PluginDescription] = {}

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    @property
    def states(self) -> Mapping[str, str]:
        """États connus des plugins, en lecture seule."""
        return MappingProxyType(self._states)

    @property
    def descriptions(self) -> Mapping[str, PluginDescription]:
        """Métadonnées connues des plugins, en lecture seule."""
        return MappingProxyType(self._descriptions)

    def state_of(self, plugin: str) -> str | None:
        """État de chargement d'un plugin, ``None`` s'il n'a jamais été vu."""
        return self._states.get(plugin)

    def description_of(self, plugin: str) -> PluginDescription | None:
        """Métadonnées d'un plugin, ``None`` s'il n'a jamais été décrit."""
        return self._descriptions.get(plugin)

    def describe(self, description: PluginDescription) -> None:
        """Enregistre les métadonnées d'un plugin, sans état de chargement.

        Le catalogue peut décrire un dossier que le chargeur n'a jamais ouvert
        (plugin non listé ou manifeste cassé) : la Console reste ainsi capable
        de le montrer sans que le démarrage en dépende.
        """
        name = description.name.strip()
        if not name:
            raise ValueError("plugin name is required")
        self._descriptions[name] = description

    def instruction_of(self, plugin: str) -> str:
        """Instruction d'usage d'un plugin, vide s'il n'en déclare pas."""
        return self._instructions.get(plugin, "")

    def tools_of(self, plugin: str) -> tuple[str, ...]:
        """Noms des Tools réellement attribués à un plugin, triés."""
        return tuple(
            sorted(
                spec.name
                for spec in self._tools.specs()
                if self._tools.owner_of(spec.name) == plugin
            )
        )

    def mark_state(
        self,
        plugin: str,
        state: str,
        *,
        manifest: PluginManifest | None = None,
        error: str = "",
    ) -> None:
        """Enregistre l'état de chargement d'un plugin.

        Les états sont ceux du glossaire ; une valeur inconnue signale une
        dérive du chargeur, pas une donnée utilisateur. Un manifeste fourni
        enrichit le catalogue (version, plateforme, description, exigences) ;
        ``error`` porte une raison lisible, jamais une valeur de Secret.
        """
        if state not in PLUGIN_STATES:
            raise ValueError(
                f"unknown plugin state {state!r} (known: {', '.join(PLUGIN_STATES)})"
            )
        name = plugin.strip()
        self._states[name] = state
        if manifest is not None:
            self._descriptions[name] = PluginDescription.from_manifest(
                manifest, error=error
            )
        elif error:
            known = self._descriptions.get(name)
            self._descriptions[name] = (
                PluginDescription(
                    name=name,
                    version=known.version if known else "",
                    platform=known.platform if known else "",
                    description=known.description if known else "",
                    config=known.config if known else (),
                    secrets=known.secrets if known else (),
                    error=error,
                )
            )

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
