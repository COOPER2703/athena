from __future__ import annotations

from typing import Awaitable, Callable, Mapping

from server.core.ports import ToolSpec

ToolHandler = Callable[[Mapping[str, object]], Awaitable[Mapping[str, object]]]


class ToolRegistry:
    """Registre pur des Tools du noyau (ADR-0008).

    Associe une déclaration (``ToolSpec``) à un handler asynchrone. Le noyau
    résout un nom de Tool appelé par le modèle, exécute le handler et renvoie son
    résultat. Un nom inconnu produit un résultat d'erreur sans jamais lever, afin
    que l'échange avec le modèle reste vivant.
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    async def execute(
        self, name: str, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        handler = self._handlers.get(name)
        if handler is None:
            return {"status": "error", "message": f"unknown tool: {name}"}
        return await handler(arguments)
