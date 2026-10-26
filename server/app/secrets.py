from __future__ import annotations

import os
from typing import Mapping

from server.core.ports import MissingSecretError, SecretResolver

# Le port vit dans le noyau (``server.core.ports``) pour qu'aucun adaptateur
# n'importe cette racine de composition (ADR-0001). Il est ré-exporté ici pour
# que les imports existants continuent de marcher.
__all__ = ["EnvSecretResolver", "MissingSecretError", "SecretResolver"]


class EnvSecretResolver:
    """Résout les secrets depuis les variables d'environnement."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if env is None else env

    def resolve(self, name: str) -> str:
        value = self._env.get(name)
        if not value:
            raise MissingSecretError(
                f"Missing required secret {name!r}: set it in the environment "
                "or in the .env file"
            )
        return value
