from __future__ import annotations

from typing import Mapping

from server.core.ports import ToolSpec
from server.core.tools import ToolRegistry

END_SESSION = "end_session"

_END_SESSION_SPEC = ToolSpec(
    name=END_SESSION,
    description=(
        "Décide de la fin de la Session quand l'échange te paraît terminé, sans "
        "attendre une demande explicite : « au revoir », « c'est tout », « non "
        "c'est bon », « nickel, merci », ou un acquiescement final après ta "
        "réponse peuvent suffire. Prononce d'abord une courte phrase d'adieu : "
        "la Session se ferme quelques secondes après. Ne l'appelle pas tant "
        "qu'une demande est en cours, pendant une hésitation, ou si l'utilisateur "
        "peut encore vouloir préciser quelque chose ; en cas de doute, laisse la "
        "Session ouverte."
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
