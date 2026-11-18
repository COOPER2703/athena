"""Lecture du fil de la Conversation pour la Console (ticket #172, ADR-0024).

Le fil n'est pas un état du noyau : c'est une **lecture paginée du Journal**.
Cette petite couture appartient à la racine de composition — elle connaît le
magasin de mémoire, l'utilisateur et la rétention, et n'expose à l'adaptateur
Console qu'un port déjà borné, sans base ni noyau.
"""

from __future__ import annotations

from server.core.ports import MemoryStore, ThreadPage


class ThreadView:
    """Implémentation du port ``ThreadReader`` sur le Journal de la mémoire.

    ``limit`` est la taille de page demandée ; le magasin est interrogé une
    entrée de plus pour que ``has_more`` dise la vérité sans requête
    supplémentaire. ``retention_days`` accompagne chaque page : le fil est borné
    à la mémoire courte, et l'interface doit le dire.
    """

    def __init__(
        self, *, memory: MemoryStore, user_id: str, retention_days: int
    ) -> None:
        self._memory = memory
        self._user_id = user_id
        self._retention_days = retention_days

    async def recent(
        self, *, limit: int, before: int | None = None
    ) -> ThreadPage:
        entries = await self._memory.recent_entries(
            self._user_id, limit=limit + 1, before=before
        )
        # ``recent_entries`` rend du plus ancien au plus récent : quand la page
        # déborde, le plus ancien de la fenêtre est celui qui reste pour la
        # requête suivante, pas celui qu'on rend.
        has_more = len(entries) > limit
        return ThreadPage(
            entries=tuple(entries[-limit:]),
            has_more=has_more,
            retention_days=self._retention_days,
        )
