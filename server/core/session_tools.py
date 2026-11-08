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
        "c'est bon », « non c'est bon en fait », « nickel, merci », ou un "
        "acquiescement final après ta réponse peuvent suffire, même formulés "
        "de façon indirecte ou hésitante. Prononce d'abord une courte phrase "
        "d'adieu : la Session se ferme immédiatement après l'appel. Ne "
        "l'appelle pas tant qu'une demande est en cours, pendant une hésitation "
        "sur une demande en cours, ou si l'utilisateur peut encore vouloir "
        "préciser quelque chose ; en cas de doute, laisse la Session ouverte."
    ),
    parameters={"type": "object", "properties": {}},
)


def register_session_tools(registry: ToolRegistry) -> ToolRegistry:
    """Enregistre le Tool du noyau ``end_session`` (ADR-0008).

    Le handler ne fait qu'accuser réception : la politique de fermeture
    (teardown immédiat, ADR-0022) reste dans le Coordinator.
    """

    async def end_session(arguments: Mapping[str, object]) -> Mapping[str, object]:
        return {"status": "ok"}

    registry.register(_END_SESSION_SPEC, end_session)
    return registry
