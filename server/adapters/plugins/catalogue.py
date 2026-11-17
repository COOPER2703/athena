"""Catalogue des Plugins installés : lecture tolérante des manifestes.

Le chargeur ne lit le manifeste que d'un plugin **activé**, et un manifeste
cassé y est fatal (ADR-0014). La Console, elle, doit pouvoir montrer tous les
dossiers présents — y compris ceux que personne n'a listés et ceux dont le
manifeste est illisible — sans que leur simple présence empêche le serveur de
démarrer.

Ce module fait donc la seule chose que le chargeur ne fait pas : parcourir le
répertoire des Plugins et décrire chaque dossier, en attrapant l'erreur de
validation au lieu de la propager. Il ne décide ni de l'activation ni du
chargement : il fournit des métadonnées à la Console.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from plugin_api import PluginManifest
from server.adapters.plugins.loader import PluginLoadError, read_manifest
from server.core.plugins import PluginDescription


@dataclass(frozen=True, slots=True)
class CatalogueEntry:
    """Un dossier de Plugin : son manifeste, ou la raison de son invalidité."""

    name: str
    manifest: PluginManifest | None
    error: str = ""

    @property
    def description(self) -> PluginDescription:
        """Métadonnées lisibles, même quand le manifeste est illisible."""
        if self.manifest is None:
            return PluginDescription(name=self.name, error=self.error)
        return PluginDescription.from_manifest(self.manifest, error=self.error)


class PluginCatalogue:
    """Scan paresseux et mis en cache du répertoire des Plugins.

    Le scan a lieu au premier accès, après la construction de la racine de
    composition : les tests peuvent déposer un plugin avant le démarrage. Il
    est ensuite figé, comme l'activation elle-même, qui ne change qu'au
    redémarrage.
    """

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        self._entries: tuple[CatalogueEntry, ...] | None = None

    def entries(self) -> tuple[CatalogueEntry, ...]:
        """Tous les dossiers de Plugin, triés par nom, manifeste lu ou erreur."""
        if self._entries is None:
            self._entries = self._scan()
        return self._entries

    def entry(self, name: str) -> CatalogueEntry | None:
        """Le dossier d'un Plugin, ``None`` s'il n'existe pas."""
        for candidate in self.entries():
            if candidate.name == name:
                return candidate
        return None

    def _scan(self) -> tuple[CatalogueEntry, ...]:
        if not self._directory.is_dir():
            return ()
        entries: list[CatalogueEntry] = []
        for path in sorted(self._directory.iterdir(), key=lambda item: item.name):
            if not path.is_dir():
                continue
            try:
                manifest = read_manifest(path.name, path)
            except PluginLoadError as exc:
                # Un manifeste cassé reste lisible : c'est la Console qui le
                # montre, pas le démarrage qui échoue.
                entries.append(
                    CatalogueEntry(name=path.name, manifest=None, error=str(exc))
                )
            else:
                entries.append(
                    CatalogueEntry(name=path.name, manifest=manifest)
                )
        return tuple(entries)
