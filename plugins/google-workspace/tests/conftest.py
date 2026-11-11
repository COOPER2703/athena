"""Rend le faux serveur et le harnais du dossier importables, sans passer par le
conftest serveur.

pytest tourne en mode ``prepend`` sans ``__init__.py`` : chaque dossier de tests
de plugin ajoute le sien au chemin pour que ``google_stub`` et
``google_workspace_harness`` soient trouvés quel que soit le point d'entrée de
pytest. Le harnais partagé, lui, vient de ``testkit`` à la racine.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
