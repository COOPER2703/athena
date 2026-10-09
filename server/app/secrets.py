from __future__ import annotations

import os
from typing import Mapping, Protocol


class MissingSecretError(RuntimeError):
    """Levée quand un secret requis est introuvable dans sa source."""


class SecretResolver(Protocol):
    """Port de résolution des secrets.

    Les adaptateurs reçoivent la valeur déjà résolue et ne lisent jamais
    l'environnement eux-mêmes (ADR-0001).
    """

    def resolve(self, name: str) -> str: ...


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
