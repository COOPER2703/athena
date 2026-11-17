"""Mesures système du bandeau de santé de la Console (ticket #177).

Le noyau est pur : c'est la couche de composition qui touche le disque. Ce
module ne calcule que des tailles de fichiers, et il ne lève jamais — une base
absente vaut ``0``, jamais une erreur qui casserait l'instantané.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

#: Suffixes que SQLite ajoute à une base ouverte (journal WAL et mémoire
#: partagée). Les compter évite de sous-estimer une base active.
_DATABASE_SUFFIXES = ("", "-wal", "-shm")


def database_sizes(paths: Mapping[str, str | Path]) -> dict[str, int]:
    """Taille sur disque de chaque base, suffixes SQLite compris.

    ``paths`` associe un nom stable (``memory``, ``auth``, ``settings``) au
    chemin de la base. Une base jamais ouverte, ou un fichier illisible, vaut
    ``0`` : l'instantané doit rester lisible même en Installation.
    """
    return {name: _file_family_bytes(Path(path)) for name, path in paths.items()}


def _file_family_bytes(path: Path) -> int:
    total = 0
    for suffix in _DATABASE_SUFFIXES:
        try:
            total += Path(f"{path}{suffix}").stat().st_size
        except OSError:
            continue
    return total
