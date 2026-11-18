"""Ré-exporte le harnais des tests du noyau (``harness.py``).

Le harnais vit dans ``harness.py``, sous un nom de module unique : les
``conftest.py`` locaux des plugins (#161) portent le même nom que ce fichier, et
pytest remplace ``sys.modules["conftest"]`` à chaque chargement. Les tests du
noyau importent donc ``harness``, jamais ``conftest`` ; ce fichier reste le point
d'entrée attendu par pytest et ré-exporte les aides pour les hooks à venir.
"""

from __future__ import annotations

from harness import *  # noqa: F401,F403
