from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from client.wake_word import (
    Detection,
    OpenWakeWordDetector,
    WakeWordUnavailableError,
)


class FakeScorer:
    """Scorer factice : aucun ONNX, aucun fichier, aucun micro."""

    def __init__(self, scores: dict[str, float] | None = None) -> None:
        self.scores = {"athena": 0.9} if scores is None else scores
        self.chunks: list[np.ndarray] = []
        self.resets = 0

    def predict(self, samples: np.ndarray) -> dict[str, float]:
        self.chunks.append(samples)
        return self.scores

    def reset(self) -> None:
        self.resets += 1


def test_detection_is_a_frozen_value_object() -> None:
    detection = Detection(score=0.8)
    assert detection.score == 0.8
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(detection, "score", 0.1)


def test_process_feeds_pcm16_samples_to_the_scorer_and_reports_a_detection() -> None:
    scorer = FakeScorer({"athena": 0.8})
    detector = OpenWakeWordDetector(threshold=0.5, scorer=scorer)

    detection = detector.process(np.array([100, -100], dtype=np.int16).tobytes())

    assert detection == Detection(score=0.8)
    assert len(scorer.chunks) == 1
    assert scorer.chunks[0].dtype == np.int16
    assert scorer.chunks[0].tolist() == [100, -100]


def test_process_returns_none_below_the_threshold() -> None:
    detector = OpenWakeWordDetector(threshold=0.5, scorer=FakeScorer({"athena": 0.4}))

    assert detector.process(b"\x00\x00" * 1280) is None


def test_process_ignores_an_empty_chunk() -> None:
    scorer = FakeScorer()
    detector = OpenWakeWordDetector(threshold=0.5, scorer=scorer)

    assert detector.process(b"") is None
    assert scorer.chunks == []


def test_close_resets_the_model_and_stops_scoring() -> None:
    scorer = FakeScorer()
    detector = OpenWakeWordDetector(threshold=0.5, scorer=scorer)

    detector.close()

    assert scorer.resets == 1
    assert detector.process(b"\x00\x00" * 1280) is None
    assert scorer.chunks == []
    detector.close()
    assert scorer.resets == 1


def test_missing_model_fails_with_a_clear_error(tmp_path) -> None:
    missing = tmp_path / "athena.onnx"

    with pytest.raises(WakeWordUnavailableError, match="athena.onnx"):
        OpenWakeWordDetector(model_path=missing, threshold=0.5)


def test_missing_feature_model_fails_with_a_clear_error(tmp_path) -> None:
    model = tmp_path / "athena.onnx"
    model.write_bytes(b"not really a model")

    with pytest.raises(WakeWordUnavailableError, match="melspectrogram"):
        OpenWakeWordDetector(
            model_path=model,
            threshold=0.5,
            melspec_model_path=tmp_path / "melspectrogram.onnx",
            embedding_model_path=tmp_path / "embedding_model.onnx",
        )
