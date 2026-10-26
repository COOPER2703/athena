"""Serveur Athena : protocole partagé, noyau pur et adaptateurs.

``__version__`` est la version exposée par l'instantané de la Console web. Elle
reste le miroir de ``pyproject.toml`` : c'est une donnée de lecture, jamais un
secret.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
