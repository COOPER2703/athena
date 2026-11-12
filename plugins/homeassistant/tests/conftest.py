"""Rend le faux serveur du dossier importable, sans passer par le conftest serveur.

pytest tourne en mode ``prepend`` sans ``__init__.py`` : chaque dossier de tests de
plugin ajoute le sien au chemin pour que ``ha_stub`` soit trouvé quel que soit le
point d'entrée de pytest. Le harnais partagé, lui, vient de ``testkit`` à la racine.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
