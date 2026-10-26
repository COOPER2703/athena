from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from server.core.ports import ToolSpec
from server.core.tools import ToolHandler, ToolRegistry

# Seule plateforme chargeable par le serveur : les plateformes clientes
# (desktop, android, ios) sont hors périmètre (ADR-0014).
SERVER_PLATFORM = "server"


@dataclass(frozen=True, slots=True)
class ConfigField:
    """Clé de configuration non secrète déclarée par un manifeste.

    ``required`` rend la clé obligatoire dans le fichier d'état : une clé
    requise absente ou vide est un défaut de configuration fatal. La valeur lue
    est toujours une chaîne (le fichier d'état stocke du JSON).
    """

    key: str
    type: str = "string"
    required: bool = False


@dataclass(frozen=True, slots=True)
class SecretField:
    """Nom d'un secret requis, jamais sa valeur (ADR-0014).

    Le manifeste ne contient que le nom ; le chargeur résout la valeur depuis
    l'environnement via le ``SecretResolver`` injecté.
    """

    key: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Description statique d'un plugin : identité, plateforme, exigences.

    Le manifeste ne liste pas les Tools (ils sont découverts à l'exécution) et
    ne déclare pas les Sources (nommées au moment où l'Événement est poussé).
    """

    api_version: int
    name: str
    version: str
    description: str
    platform: str
    entrypoint: str
    config: tuple[ConfigField, ...] = ()
    secrets: tuple[SecretField, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolBinding:
    """Un Tool déclaré par un plugin : sa déclaration et son handler."""

    spec: ToolSpec
    handler: ToolHandler


@dataclass(frozen=True, slots=True)
class PluginBinding:
    """Ce qu'un plugin rend au serveur : ses Tools et son instruction d'usage.

    ``name`` est le nom du plugin (celui du manifeste) : c'est lui qui devient
    le propriétaire de chaque Tool dans le registre. Le plugin n'écrit jamais
    lui-même dans le registre ; le noyau agrège (ADR-0014).
    """

    name: str
    tools: tuple[ToolBinding, ...]
    instruction: str = ""


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Contexte injecté à ``create(context)`` : tout ce qu'un plugin reçoit.

    Un plugin n'importe aucun interne du serveur. ``config`` porte la
    configuration non secrète déclarée par le manifeste, ``secrets`` les valeurs
    déjà résolues (jamais tracées), ``trace`` le flux d'observabilité préfixé
    par le nom du plugin, et ``push_event`` l'entrée du Canal d'événements.
    """

    name: str
    config: Mapping[str, str]
    secrets: Mapping[str, str]
    trace: Callable[..., None]
    push_event: Callable[[str, str], None]


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
