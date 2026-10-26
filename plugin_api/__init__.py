"""Contrat public des plugins serveur (ADR-0014).

Un plugin n'importe **aucun** interne du serveur : ce module est la seule
surface qu'il connaît. Il porte les types que le serveur injecte
(``PluginContext``), ceux que le plugin rend (``PluginBinding``), la description
de son manifeste et l'erreur qui signale une plateforme injoignable.

Le module reste **pur** : aucune I/O, aucun asyncio, aucune dépendance au
serveur. Le noyau le ré-exporte (``server.core.plugins``, ``server.core.ports``,
``server.core.tools``) pour que les imports existants continuent de marcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping

# Seule plateforme chargeable par le serveur : les plateformes clientes
# (desktop, android, ios) sont hors périmètre (ADR-0014).
SERVER_PLATFORM = "server"
# Plateformes qu'un manifeste peut déclarer. Une valeur inconnue est une faute
# de frappe dans le manifeste, jamais une plateforme future.
KNOWN_PLATFORMS = ("server", "desktop", "android", "ios")


class PluginUnavailable(RuntimeError):
    """La plateforme du plugin est injoignable ou refuse l'authentification.

    C'est la **seule** panne que le chargeur considère comme réparable : un
    démarrage dégradé est tracé ``failed`` et une retente de fond réessaie plus
    tard (ADR-0014, ticket #57). Toute autre exception de ``create()`` est un
    défaut du plugin, donc fatal.
    """


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Pure description of a Tool a model may call (registry seam, issue #28)."""

    name: str
    description: str
    parameters: Mapping[str, object]


ToolHandler = Callable[[Mapping[str, object]], Awaitable[Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class ConfigField:
    """Clé de configuration non secrète déclarée par un manifeste.

    ``required`` rend la clé obligatoire dans le fichier d'état : une clé
    requise absente ou vide est un défaut de configuration fatal. ``type`` dit
    comment valider la valeur : ``string`` (texte non vide) ou ``url`` (URL
    ``http``/``https`` avec un hôte). La valeur lue est toujours une chaîne (le
    fichier d'état stocke du JSON).
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


__all__ = [
    "KNOWN_PLATFORMS",
    "SERVER_PLATFORM",
    "ConfigField",
    "PluginBinding",
    "PluginContext",
    "PluginManifest",
    "PluginUnavailable",
    "SecretField",
    "ToolBinding",
    "ToolHandler",
    "ToolSpec",
]
