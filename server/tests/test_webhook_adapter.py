from __future__ import annotations

import asyncio

import aiohttp
from conftest import RecordingTraceSink

from server.adapters.webhook import (
    DEFAULT_SOURCE,
    EVENTS_PATH,
    MAX_CONTENT_CHARS,
    WebhookAdapter,
)


class _Recorder:
    """Couture ``push`` de test : enregistre les faits reçus dans l'ordre."""

    def __init__(self) -> None:
        self.pushes: list[tuple[str, str]] = []

    def __call__(self, source: str, content: str) -> None:
        self.pushes.append((source, content))


def _url(adapter: WebhookAdapter, path: str = EVENTS_PATH) -> str:
    return f"http://127.0.0.1:{adapter.port}{path}"


def test_valid_event_is_accepted_pushed_and_traced() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        sink = RecordingTraceSink()
        adapter = WebhookAdapter(port=0, trace_sink=sink)
        await adapter.start(recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    json={"source": "matrix", "content": "porte ouverte"},
                ) as response:
                    assert response.status == 202
                    assert await response.json() == {"status": "accepted"}
            assert recorder.pushes == [("matrix", "porte ouverte")]
            [entry] = sink.of_kind("event_received")
            assert entry.source == "webhook"
            assert entry.payload == {"source": "matrix", "chars": 13}
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_missing_source_falls_back_to_default() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = WebhookAdapter(port=0)
        await adapter.start(recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter), json={"content": "porte ouverte"}
                ) as response:
                    assert response.status == 202
            assert recorder.pushes == [(DEFAULT_SOURCE, "porte ouverte")]
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_blank_content_is_rejected() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = WebhookAdapter(port=0)
        await adapter.start(recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    json={"source": "matrix", "content": "   "},
                ) as response:
                    assert response.status == 400
            assert recorder.pushes == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_blank_source_is_rejected() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = WebhookAdapter(port=0)
        await adapter.start(recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    json={"source": "  ", "content": "porte ouverte"},
                ) as response:
                    assert response.status == 400
            assert recorder.pushes == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_non_object_or_non_string_fields_are_rejected() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = WebhookAdapter(port=0)
        await adapter.start(recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter), json=["not", "an", "object"]
                ) as response:
                    assert response.status == 400
                async with session.post(
                    _url(adapter), json={"source": "matrix", "content": 12}
                ) as response:
                    assert response.status == 400
            assert recorder.pushes == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_invalid_json_is_rejected() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = WebhookAdapter(port=0)
        await adapter.start(recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    data="{not json",
                    headers={"Content-Type": "application/json"},
                ) as response:
                    assert response.status == 400
            assert recorder.pushes == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_non_json_content_type_is_rejected() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = WebhookAdapter(port=0)
        await adapter.start(recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    data='{"content": "porte ouverte"}',
                    headers={"Content-Type": "text/plain"},
                ) as response:
                    assert response.status == 415
            assert recorder.pushes == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_oversized_content_is_rejected_without_pushing() -> None:
    async def scenario() -> None:
        recorder = _Recorder()
        adapter = WebhookAdapter(port=0)
        await adapter.start(recorder)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter),
                    json={"source": "matrix", "content": "x" * (MAX_CONTENT_CHARS + 1)},
                ) as response:
                    assert response.status == 413
            assert recorder.pushes == []
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_unknown_path_is_not_found() -> None:
    async def scenario() -> None:
        adapter = WebhookAdapter(port=0)
        await adapter.start(_Recorder())
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(adapter, "/unknown"), json={"content": "x"}
                ) as response:
                    assert response.status == 404
        finally:
            await adapter.stop()

    asyncio.run(scenario())


def test_wrong_method_is_not_allowed() -> None:
    async def scenario() -> None:
        adapter = WebhookAdapter(port=0)
        await adapter.start(_Recorder())
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_url(adapter)) as response:
                    assert response.status == 405
        finally:
            await adapter.stop()

    asyncio.run(scenario())
