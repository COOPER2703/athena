from __future__ import annotations

from typing import Mapping

from server.core.ports import ToolSpec
from server.core.tools import ToolRegistry

END_SESSION = "end_session"

_END_SESSION_SPEC = ToolSpec(
    name=END_SESSION,
    description=(
        "Demande la fin de la Session quand l'utilisateur a clairement indiqué "
        "que la conversation est terminée (au revoir, c'est tout, merci, à "
        "bientôt) ou demande explicitement d'arrêter. Prononce d'abord tes mots "
        "d'adieu : la Session se ferme quelques secondes après. N'appelle pas ce "
        "Tool tant qu'une demande est en cours."
    ),
    parameters={"type": "object", "properties": {}},
)


def register_session_tools(registry: ToolRegistry) -> ToolRegistry:
    """Enregistre le Tool du noyau ``end_session`` (ADR-0008).

    Le handler ne fait qu'accuser réception : la politique de fermeture
    (clôture en attente, grâce) reste dans le Coordinator.
    """

    async def end_session(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return {"status": "ok"}

    registry.register(_END_SESSION_SPEC, end_session)
    return registry
