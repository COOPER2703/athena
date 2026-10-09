from __future__ import annotations

import asyncio
from typing import Any, Protocol

from client.config import AudioConfig


class Audio(Protocol):
    """Interface audio consommée par l'orchestrateur."""

    async def start_input(self) -> None: ...

    async def start_output(self) -> None: ...

    async def read_chunk(self) -> bytes: ...

    async def write_chunk(self, data: bytes) -> None: ...

    async def close(self) -> None: ...


class AudioIO:
    """Adaptateur PyAudio.

    PyAudio est importé paresseusement : le module reste importable sans
    PortAudio installé, et seuls les tests avec un vrai micro/speaker le
    chargent. Les appels bloquants passent par ``asyncio.to_thread`` pour
    respecter la boucle asyncio unique (ADR-0005).
    """

    def __init__(self, cfg: AudioConfig) -> None:
        self._cfg = cfg
        self._pya: Any = None
        self._input_stream: Any = None
        self._output_stream: Any = None

    def _pyaudio(self) -> Any:
        import pyaudio

        return pyaudio

    def _ensure_pyaudio(self) -> Any:
        if self._pya is None:
            self._pya = self._pyaudio().PyAudio()
        return self._pya

    async def start_input(self) -> None:
        pyaudio = self._pyaudio()
        pya = self._ensure_pyaudio()
        mic_info = pya.get_default_input_device_info()
        self._input_stream = await asyncio.to_thread(
            pya.open,
            format=pyaudio.paInt16,
            channels=self._cfg.channels,
            rate=self._cfg.send_sample_rate,
            input=True,
            input_device_index=int(mic_info["index"]),
            frames_per_buffer=self._cfg.chunk_size,
        )

    async def start_output(self) -> None:
        pyaudio = self._pyaudio()
        pya = self._ensure_pyaudio()
        self._output_stream = await asyncio.to_thread(
            pya.open,
            format=pyaudio.paInt16,
            channels=self._cfg.channels,
            rate=self._cfg.receive_sample_rate,
            output=True,
        )

    async def read_chunk(self) -> bytes:
        if self._input_stream is None:
            raise RuntimeError("Input stream not started")
        return await asyncio.to_thread(
            self._input_stream.read,
            self._cfg.chunk_size,
            exception_on_overflow=False,
        )

    async def write_chunk(self, data: bytes) -> None:
        if self._output_stream is None:
            raise RuntimeError("Output stream not started")
        await asyncio.to_thread(self._output_stream.write, data)

    async def close(self) -> None:
        if self._input_stream:
            await asyncio.to_thread(self._input_stream.close)
            self._input_stream = None
        if self._output_stream:
            await asyncio.to_thread(self._output_stream.close)
            self._output_stream = None
        if self._pya:
            self._pya.terminate()
            self._pya = None
