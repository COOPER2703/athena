"""Validation réelle et bon marché d'une clé Gemini avant enregistrement.

L'ADR-0025 exige un appel réel : une faute de frappe dans la Console ne doit pas
se découvrir à la première Session. ``count_tokens`` ne facture aucun jeton de
sortie et prouve que la clé est acceptée par le service.

La fabrique de client est injectable : les tests n'atteignent jamais le réseau.
Le message d'erreur ne contient jamais la clé, ni une URL, ni le corps d'une
réponse distante.
"""

from __future__ import annotations

from typing import Any, Callable

from server.core.settings import GEMINI_SECRET

# Modèle utilisé pour la sonde : Flash-Lite est déjà le défaut du juge, donc
# disponible sur l'échelle gratuite.
DEFAULT_VALIDATION_MODEL = "gemini-3.1-flash-lite"

ClientFactory = Callable[[str], Any]


def _default_client_factory(api_key: str) -> Any:
    from google import genai

    return genai.Client(api_key=api_key)


class GeminiKeyValidator:
    """``SecretValidator`` de la clé Gemini, adossé à un appel réel."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_VALIDATION_MODEL,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._model = model
        self._client_factory = client_factory or _default_client_factory

    async def validate(self, name: str, value: str) -> str | None:
        if name != GEMINI_SECRET:
            # Les autres Secrets ne savent pas être vérifiés à distance.
            return None
        return await _check(self._client_factory, value, self._model)


async def _check(
    client_factory: ClientFactory, api_key: str, model: str
) -> str | None:
    import asyncio

    def probe() -> None:
        client = client_factory(api_key)
        # Aucun jeton de sortie : la requête la moins chère qui prouve la clé.
        client.models.count_tokens(model=model, contents="ping")

    try:
        await asyncio.to_thread(probe)
    except Exception:
        return (
            "Cette clé Gemini est refusée par le service : vérifiez la clé "
            "et réessayez."
        )
    return None
