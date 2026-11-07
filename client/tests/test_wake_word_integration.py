from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from client.config import DEFAULT_WAKE_WORD_MODEL
from client.wake_word import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_MELSPEC_MODEL,
    DEFAULT_VAD_MODEL,
    OpenWakeWordDetector,
    SileroVad,
)

_MODEL_FILES = (
    Path(DEFAULT_WAKE_WORD_MODEL),
    Path(DEFAULT_MELSPEC_MODEL),
    Path(DEFAULT_EMBEDDING_MODEL),
)

# « Athéna » synthétisé par Piper (fr_FR-siwis-medium) : voir data/ATTRIBUTION.md.
_POSITIVE_WAV = Path(__file__).resolve().parent / "data" / "athena.wav"

pytestmark = pytest.mark.skipif(
    not all(path.is_file() for path in _MODEL_FILES),
    reason="modèles ONNX absents (assets non installés)",
)


def _write_silence_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1280)


def test_scores_a_16khz_mono_wav_offline(tmp_path: Path) -> None:
    wav = tmp_path / "silence.wav"
    _write_silence_wav(wav)
    with wave.open(str(wav), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        frames = handle.readframes(handle.getnframes())

    detector = OpenWakeWordDetector(DEFAULT_WAKE_WORD_MODEL, threshold=0.0)
    try:
        detection = detector.process(frames)
    finally:
        detector.close()

    assert detection is not None
    assert 0.0 <= detection.score <= 1.0


def test_the_real_model_does_not_trigger_on_silence(tmp_path: Path) -> None:
    wav = tmp_path / "silence.wav"
    _write_silence_wav(wav)
    with wave.open(str(wav), "rb") as handle:
        frames = handle.readframes(handle.getnframes())

    detector = OpenWakeWordDetector(DEFAULT_WAKE_WORD_MODEL, threshold=0.99)
    try:
        detection = detector.process(frames)
    finally:
        detector.close()

    assert detection is None


requires_positive_wav = pytest.mark.skipif(
    not _POSITIVE_WAV.is_file(),
    reason="fixture athena.wav absente (assets de test non installés)",
)


@requires_positive_wav
def test_the_real_model_detects_the_athena_fixture() -> None:
    """Vrai classifieur, vrais modèles de features, chunks réels du WAV.

    Le micro n'est jamais sollicité : l'audio vient de la fixture versionnée.
    C'est un test d'intégration du chargement et de l'inférence, pas une
    validation de prononciation humaine.
    """
    with wave.open(str(_POSITIVE_WAV), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000
        frames = handle.readframes(handle.getnframes())

    detector = OpenWakeWordDetector(DEFAULT_WAKE_WORD_MODEL, threshold=0.5)
    chunk_bytes = 1280 * 2
    detection = None
    try:
        for offset in range(0, len(frames), chunk_bytes):
            detection = detector.process(frames[offset : offset + chunk_bytes])
            if detection is not None:
                break
    finally:
        detector.close()

    assert detection is not None, "le vrai modèle n'a pas détecté « Athéna »"
    assert detection.score >= 0.5
    assert 0 < len(detection.pre_roll) <= 9600  # 300 ms de PCM16 à 16 kHz


requires_vad = pytest.mark.skipif(
    not Path(DEFAULT_VAD_MODEL).is_file(),
    reason="modèle Silero VAD absent (assets non installés)",
)


@requires_vad
def test_the_real_vad_accepts_arbitrary_chunk_sizes() -> None:
    vad = SileroVad(DEFAULT_VAD_MODEL)
    try:
        for size in (1, 300, 1024, 9600):
            score = vad.predict(np.zeros(size, dtype=np.int16))
            assert 0.0 <= score <= 1.0
    finally:
        vad.close()


@requires_vad
def test_the_real_vad_gate_rejects_silence(tmp_path: Path) -> None:
    wav = tmp_path / "silence.wav"
    _write_silence_wav(wav)
    with wave.open(str(wav), "rb") as handle:
        frames = handle.readframes(handle.getnframes())

    detector = OpenWakeWordDetector(
        DEFAULT_WAKE_WORD_MODEL, threshold=0.0, vad=True
    )
    try:
        detection = detector.process(frames)
    finally:
        detector.close()

    assert detection is None
