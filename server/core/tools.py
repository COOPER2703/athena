from __future__ import annotations

from typing import Mapping

from plugin_api import ToolHandler, ToolSpec

# ``ToolSpec`` et ``ToolHandler`` appartiennent au contrat public (``plugin_api``)
# et sont ré-exportés ici pour les imports existants du noyau.

__all__ = ["ToolHandler", "ToolNameConflict", "ToolRegistry", "ToolSpec"]


class ToolNameConflict(ValueError):
    """Deux propriétaires revendiquent le même nom de Tool.

    Une collision est fatale : le démarrage échoue plutôt que de laisser un
    plugin écraser silencieusement le Tool d'un autre (ADR-0014).
    """


class ToolRegistry:
    """Registre pur des Tools servis au modèle (ADR-0008).

    Associe une déclaration (``ToolSpec``) à un handler asynchrone et à son
    propriétaire (``core`` pour un Tool du noyau, le nom du plugin sinon). Le
    noyau résout un nom de Tool appelé par le modèle, exécute le handler et
    renvoie son résultat. Un nom inconnu ou un handler qui lève produit un
    résultat d'erreur sans jamais lever, afin que l'échange avec le modèle
    reste vivant.
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._owners: dict[str, str] = {}

    def register(
        self, spec: ToolSpec, handler: ToolHandler, *, owner: str = "core"
    ) -> None:
        existing = self._owners.get(spec.name)
        if existing is not None:
            raise ToolNameConflict(
                f"tool {spec.name!r} is already registered by {existing!r}; "
                f"cannot register it for {owner!r}"
            )
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler
        self._owners[spec.name] = owner

    def owner_of(self, name: str) -> str | None:
        """Propriétaire d'un Tool enregistré, ``None`` si le nom est inconnu."""
        return self._owners.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    async def execute(
        self, name: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        handler = self._handlers.get(name)
        if handler is None:
            return {"status": "error", "message": f"unknown tool: {name}"}
        try:
            return await handler(arguments)
        except Exception as exc:
            # Un handler de plugin est du code externe : sa panne devient un
            # résultat d'erreur rendu au modèle, jamais une Session qui tombe.
            return {"status": "error", "message": f"tool {name} failed: {exc}"}
