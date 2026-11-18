"""Résolution des Secrets : environnement du processus, puis magasin, puis ``.env``.

L'ADR-0025 fixe la précédence : **environnement > magasin > ``.env`` > défauts**.
Le fichier ``.env`` doit donc rester une couche distincte de l'environnement, et
non une simple fusion : un Secret saisi dans la Console ne doit pas être masqué
par une vieille valeur du fichier, tandis qu'un Secret d'environnement garde la
priorité absolue (porte de sortie de secours).

Le port ``SecretResolver`` reste celui du noyau : les plugins résolvent leurs
Secrets par la même instance, donc le magasin est lu après l'environnement
(amendement de l'ADR-0014). Aucune valeur n'est tracée.
"""

from __future__ import annotations

from typing import Mapping

from server.core.ports import MissingSecretError, SecretStore


class LayeredSecretResolver:
    """Résolveur à trois couches, sans jamais exposer la provenance dans un log."""

    def __init__(
        self,
        *,
        process_env: Mapping[str, str] | None = None,
        store: SecretStore | None = None,
        dotenv: Mapping[str, str] | None = None,
    ) -> None:
        self._process_env = process_env or {}
        self._store = store
        self._dotenv = dotenv or {}

    def optional(self, name: str) -> str | None:
        """Valeur résolue, ou ``None`` : une chaîne vide compte comme absente."""
        value = self._process_env.get(name)
        if value:
            return value
        if self._store is not None:
            stored = self._store.secret(name)
            if stored:
                return stored
        value = self._dotenv.get(name)
        return value or None

    def resolve(self, name: str) -> str:
        value = self.optional(name)
        if value is None:
            raise MissingSecretError(
                f"Missing required secret {name!r}: set it in the environment, "
                "in the settings store or in the .env file"
            )
        return value
