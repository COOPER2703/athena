from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from client.audio_io import AudioIO
from client.config import AudioConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeStream:
    def __init__(self) -> None:
        self.read_calls: list[tuple[int, bool]] = []
        self.writes: list[bytes] = []
        self.stopped = False
        self.started = False
        self.closed = False
        self.events: list[str] = []

    def read(self, frames: int, exception_on_overflow: bool = True) -> bytes:
        self.read_calls.append((frames, exception_on_overflow))
        self.events.append("read")
        return b"\x00" * frames

    def write(self, data: bytes) -> None:
        self.events.append("write")
        self.writes.append(data)

    def stop_stream(self) -> None:
        self.events.append("stop")
        self.stopped = True

    def start_stream(self) -> None:
        self.events.append("start")
        self.started = True

    def close(self) -> None:
        self.events.append("close")
        self.closed = True


class FakePyAudio:
    paInt16 = 8

    def __init__(self, stream_factory: Any = FakeStream) -> None:
        self.opens: list[dict[str, Any]] = []
        self.streams: list[FakeStream] = []
        self.terminated = False
        self._stream_factory = stream_factory

    def get_default_input_device_info(self) -> dict[str, Any]:
        return {"index": 0}

    def open(self, **kwargs: Any) -> FakeStream:
        stream = self._stream_factory()
        self.opens.append(kwargs)
        self.streams.append(stream)
        return stream

    def terminate(self) -> None:
        self.terminated = True


def make_fake_pyaudio(
    stream_factory: Any = FakeStream,
) -> tuple[SimpleNamespace, list[FakePyAudio]]:
    instances: list[FakePyAudio] = []

    def factory() -> FakePyAudio:
        instance = FakePyAudio(stream_factory)
        instances.append(instance)
        return instance

    module = SimpleNamespace(PyAudio=factory, paInt16=FakePyAudio.paInt16)
    return module, instances


def test_module_import_does_not_require_pyaudio() -> None:
    code = (
        "import sys; "
        "sys.modules['pyaudio'] = None; "
        "import client.audio_io; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_capture_opens_pcm16_mono_16khz_in_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, instances = make_fake_pyaudio()
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    audio = AudioIO(AudioConfig(chunk_size=512))

    asyncio.run(audio.start_input())

    opened = instances[-1].opens[-1]
    assert opened["format"] == module.paInt16
    assert opened["channels"] == 1
    assert opened["rate"] == 16000
    assert opened["frames_per_buffer"] == 512
    assert opened["input"] is True


def test_read_chunk_reads_configured_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, instances = make_fake_pyaudio()
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    audio = AudioIO(AudioConfig(chunk_size=512))
    asyncio.run(audio.start_input())

    data = asyncio.run(audio.read_chunk())

    assert data == b"\x00" * 512
    assert instances[-1].streams[-1].read_calls == [(512, False)]


def test_playback_opens_pcm16_mono_24khz(monkeypatch: pytest.MonkeyPatch) -> None:
    module, instances = make_fake_pyaudio()
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    audio = AudioIO(AudioConfig())

    asyncio.run(audio.start_output())
    asyncio.run(audio.write_chunk(b"\x01\x02"))

    opened = instances[-1].opens[-1]
    assert opened["format"] == module.paInt16
    assert opened["channels"] == 1
    assert opened["rate"] == 24000
    assert opened["output"] is True
    assert instances[-1].streams[-1].writes == [b"\x01\x02"]


def test_flush_stops_and_restarts_output_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, instances = make_fake_pyaudio()
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    audio = AudioIO(AudioConfig())
    asyncio.run(audio.start_output())

    asyncio.run(audio.flush())

    stream = instances[-1].streams[-1]
    assert stream.stopped
    assert stream.started


def test_flush_without_output_stream_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, instances = make_fake_pyaudio()
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    audio = AudioIO(AudioConfig())

    asyncio.run(audio.flush())

    assert instances == []


def test_close_releases_streams_and_portaudio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, instances = make_fake_pyaudio()
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    audio = AudioIO(AudioConfig())
    asyncio.run(audio.start_input())
    asyncio.run(audio.start_output())

    asyncio.run(audio.close())

    pya = instances[-1]
    assert all(stream.closed for stream in pya.streams)
    assert pya.terminated


class BlockingWriteStream(FakeStream):
    """Écriture qui bloque, pour prouver que ``flush`` attend son tour."""

    def __init__(self) -> None:
        super().__init__()
        self.write_started = threading.Event()
        self.release = threading.Event()

    def write(self, data: bytes) -> None:
        self.events.append("write-start")
        self.write_started.set()
        self.release.wait(timeout=1)
        self.events.append("write-end")
        self.writes.append(data)


def test_flush_waits_for_an_in_flight_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``flush`` ne stoppe pas le flux pendant une écriture : pas de course PortAudio."""
    module, instances = make_fake_pyaudio(BlockingWriteStream)
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    audio = AudioIO(AudioConfig())

    async def scenario() -> None:
        await audio.start_output()
        stream = instances[-1].streams[-1]

        write_task = asyncio.create_task(audio.write_chunk(b"\x01"))
        await asyncio.to_thread(stream.write_started.wait, 1)
        flush_task = asyncio.create_task(audio.flush())
        await asyncio.sleep(0.05)

        assert "stop" not in stream.events

        stream.release.set()
        await write_task
        await flush_task

        assert stream.events.index("stop") > stream.events.index("write-end")

    asyncio.run(scenario())


def test_close_stops_the_input_stream_before_closing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Arrêter le flux débloque le ``read`` en cours avant ``close``."""
    module, instances = make_fake_pyaudio()
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    audio = AudioIO(AudioConfig())
    asyncio.run(audio.start_input())

    asyncio.run(audio.close())

    stream = instances[-1].streams[-1]
    assert stream.events.index("stop") < stream.events.index("close")


class FailingReadStream(FakeStream):
    def read(self, frames: int, exception_on_overflow: bool = True) -> bytes:
        raise OSError("PaAlsaStream_SetUpBuffers failed")


def test_read_chunk_reopens_the_input_after_a_portaudio_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un échec ALSA transitoire rouvre le micro au lieu de tuer le Client."""
    opened = 0

    def stream_factory() -> FakeStream:
        nonlocal opened
        opened += 1
        return FailingReadStream() if opened == 1 else FakeStream()

    module, instances = make_fake_pyaudio(stream_factory)
    monkeypatch.setitem(sys.modules, "pyaudio", module)
    audio = AudioIO(AudioConfig(chunk_size=512))
    asyncio.run(audio.start_input())

    data = asyncio.run(audio.read_chunk())

    assert data == b"\x00" * 512
    assert len(instances[-1].streams) == 2
