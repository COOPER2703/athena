"""Réveil asyncio du Planificateur (ADR-0028, #195).

La boucle du noyau ``Planificateur`` ne connaît que le port ``Wakeup``. Cet
adaptateur possède l'``asyncio.Event`` : une écriture de Planification appelle
``notify`` (synchrone, depuis la route du Tool), et la boucle attend soit
l'échéance calculée, soit le réveil. Un ``wait`` rend la main à l'échéance
**comme** au réveil, sans lever : l'appelant relit toujours le magasin.
"""

from __future__ import annotations

import asyncio


class AsyncioWakeup:
    """``Wakeup`` adossé à un ``asyncio.Event``, réutilisé à chaque attente."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def notify(self) -> None:
        """Réveille la boucle dès la prochaine occasion (jamais bloquant)."""
        self._event.set()

    async def wait(self, timeout: float | None = None) -> None:
        """Dort au plus ``timeout`` secondes, ou jusqu'au prochain ``notify``."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            # Le réveil est consommé : la boucle relit le magasin de toute façon
            # et recalcule son échéance, donc un réveil perdu ne perd aucune
            # Planification, seulement un tour de boucle.
            self._event.clear()
