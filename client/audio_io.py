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

    async def flush(self) -> None: ...

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
        # PortAudio n'est pas sûr quand on stoppe/écrit le même flux depuis
        # deux threads : ``flush`` attend la fin de l'écriture en cours.
        self._output_lock = asyncio.Lock()
        self._closed = False

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
        stream = self._input_stream
        if stream is None:
            raise RuntimeError("Input stream not started")
        try:
            return await asyncio.to_thread(
                stream.read,
                self._cfg.chunk_size,
                exception_on_overflow=False,
            )
        except OSError:
            # Un échec ALSA/PipeWire est transitoire : rouvrir le micro évite
            # de laisser le Client avec un flux mort.
            await self._reopen_input()
            stream = self._input_stream
            if stream is None:
                raise
            return await asyncio.to_thread(
                stream.read,
                self._cfg.chunk_size,
                exception_on_overflow=False,
            )

    async def _reopen_input(self) -> None:
        if self._closed:
            raise RuntimeError("Audio closed")
        stream = self._input_stream
        self._input_stream = None
        if stream is not None:
            try:
                await asyncio.to_thread(stream.close)
            except Exception:
                pass
        await self.start_input()

    async def write_chunk(self, data: bytes) -> None:
        async with self._output_lock:
            stream = self._output_stream
            if stream is None:
                raise RuntimeError("Output stream not started")
            await asyncio.to_thread(stream.write, data)

    async def flush(self) -> None:
        async with self._output_lock:
            stream = self._output_stream
            if stream is None:
                return
            await asyncio.to_thread(stream.stop_stream)
            await asyncio.to_thread(stream.start_stream)

    async def close(self) -> None:
        self._closed = True
        input_stream = self._input_stream
        self._input_stream = None
        if input_stream:
            # Arrêter d'abord débloque le ``read`` éventuellement en cours ;
            # fermer pendant une lecture provoque les erreurs ALSA de teardown.
            try:
                await asyncio.to_thread(input_stream.stop_stream)
            except Exception:
                pass
            await asyncio.to_thread(input_stream.close)
        async with self._output_lock:
            output_stream = self._output_stream
            self._output_stream = None
            if output_stream:
                try:
                    await asyncio.to_thread(output_stream.stop_stream)
                except Exception:
                    pass
                await asyncio.to_thread(output_stream.close)
        if self._pya:
            self._pya.terminate()
            self._pya = None
