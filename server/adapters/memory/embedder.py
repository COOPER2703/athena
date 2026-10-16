from __future__ import annotations

import asyncio
import threading
from typing import Any

DEFAULT_EMBED_MODEL = "all-MiniLM-L6-v2"


class SentenceTransformerEmbedder:
    """Async ``Embedder`` backed by a local sentence-transformers model.

    The CPU work always runs off the event loop via ``asyncio.to_thread``. The
    model is imported and loaded lazily on the first embed, and preloaded at
    startup through ``warmup``. The local cache is tried first, so the server
    can embed offline.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBED_MODEL,
        *,
        model: Any | None = None,
    ) -> None:
        self._model_name = model_name
        self._model = model
        self._lock = threading.Lock()

    def _load(self) -> Any:
        from sentence_transformers import SentenceTransformer

        try:
            return SentenceTransformer(self._model_name, local_files_only=True)
        except Exception:
            return SentenceTransformer(self._model_name)

    def _get_model(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._model = self._load()
        return self._model

    def _encode(self, text: str) -> list[float]:
        return self._get_model().encode(text).tolist()

    async def embed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._encode, text)

    async def warmup(self) -> None:
        await asyncio.to_thread(self._get_model)
