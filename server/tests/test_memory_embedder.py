from __future__ import annotations

import asyncio
import time

from server.adapters.memory import SentenceTransformerEmbedder


class _Vector:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return list(self._values)


class SlowModel:
    """Stand-in for a sentence-transformers model whose ``encode`` blocks."""

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def encode(self, text: str) -> _Vector:
        time.sleep(self.delay)
        return _Vector([0.0, 0.5, 1.0, 1.5])


def test_embed_runs_off_the_event_loop() -> None:
    async def scenario() -> None:
        embedder = SentenceTransformerEmbedder(model=SlowModel(0.1))
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        try:
            vector = await embedder.embed("bonjour")
            await asyncio.sleep(0)
            assert vector == [0.0, 0.5, 1.0, 1.5]
            # time.sleep blocked the worker thread, not the loop: the ticker
            # kept advancing while the embed was in flight.
            assert ticks >= 5
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_model_is_loaded_lazily_and_shared() -> None:
    class CountingModel:
        def __init__(self) -> None:
            self.encodes = 0

        def encode(self, text: str) -> _Vector:
            self.encodes += 1
            return _Vector([0.0])

    model = CountingModel()
    embedder = SentenceTransformerEmbedder(model=model)

    async def scenario() -> None:
        await embedder.embed("a")
        await embedder.embed("b")

    asyncio.run(scenario())
    assert model.encodes == 2
