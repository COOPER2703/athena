from __future__ import annotations

import dataclasses
import logging

import numpy as np
import pytest

from client.wake_word import (
    Detection,
    OpenWakeWordDetector,
    WakeWordUnavailableError,
)


class FakeScorer:
    """Scorer factice : aucun ONNX, aucun fichier, aucun micro."""

    def __init__(
        self,
        scores: dict[str, float] | None = None,
        *,
        score_sequence: list[float] | None = None,
    ) -> None:
        self.scores = {"athena": 0.9} if scores is None else scores
        self.score_sequence = score_sequence
        self.chunks: list[np.ndarray] = []
        self.resets = 0
        self.closes = 0

    def predict(self, samples: np.ndarray) -> dict[str, float]:
        self.chunks.append(samples)
        if self.score_sequence is not None:
            score = self.score_sequence.pop(0)
            return {"athena": score}
        return self.scores

    def reset(self) -> None:
        self.resets += 1

    def close(self) -> None:
        self.closes += 1


class FakeVad:
    """VAD factice : aucune dépendance Silero, aucun ONNX."""

    def __init__(self, speech: float = 0.9) -> None:
        self.speech = speech
        self.chunks: list[np.ndarray] = []
        self.closes = 0

    def predict(self, samples: np.ndarray) -> float:
        self.chunks.append(samples)
        return self.speech

    def close(self) -> None:
        self.closes += 1


def _pcm(samples: int, value: int = 1) -> bytes:
    return np.full(samples, value, dtype=np.int16).tobytes()


def test_detection_is_a_frozen_value_object() -> None:
    detection = Detection(score=0.8)
    assert detection.score == 0.8
    assert detection.pre_roll == b""
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(detection, "score", 0.1)


def test_process_feeds_pcm16_samples_to_the_scorer_and_reports_a_detection() -> None:
    scorer = FakeScorer({"athena": 0.8})
    detector = OpenWakeWordDetector(threshold=0.5, scorer=scorer)

    chunk = np.array([100, -100], dtype=np.int16).tobytes()

    detection = detector.process(chunk)

    assert detection == Detection(score=0.8, pre_roll=chunk)
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


def test_arbitrary_chunk_sizes_reach_the_scorer_without_loss() -> None:
    scorer = FakeScorer(score_sequence=[0.1, 0.1, 0.1, 0.9])
    detector = OpenWakeWordDetector(threshold=0.5, scorer=scorer)
    chunks = [_pcm(1024, 1), _pcm(1, 2), _pcm(3000, 3), _pcm(17, 4)]

    results = [detector.process(chunk) for chunk in chunks]

    assert results[:3] == [None, None, None]
    assert results[3] is not None
    fed = b"".join(chunk.tobytes() for chunk in scorer.chunks)
    assert fed == b"".join(chunks)


def test_a_detection_carries_the_pre_roll_ending_with_the_current_chunk() -> None:
    scorer = FakeScorer(score_sequence=[0.1, 0.1, 0.9])
    detector = OpenWakeWordDetector(
        threshold=0.5, pre_roll_ms=300, scorer=scorer
    )
    first = _pcm(2500, 11)
    second = _pcm(1500, 22)
    third = _pcm(2000, 33)

    assert detector.process(first) is None
    assert detector.process(second) is None
    detection = detector.process(third)

    assert detection is not None
    assert detection.pre_roll == (first + second + third)[-9600:]


def test_the_pre_roll_is_bounded_and_keeps_the_tail_of_a_huge_chunk() -> None:
    scorer = FakeScorer({"athena": 0.9})
    detector = OpenWakeWordDetector(
        threshold=0.5, pre_roll_ms=300, scorer=scorer
    )
    chunk = bytes(range(256)) * 80

    detection = detector.process(chunk)

    assert detection is not None
    assert len(detection.pre_roll) == 9600
    assert detection.pre_roll == chunk[-9600:]


def test_the_pre_roll_accumulates_chunks_smaller_than_it() -> None:
    scorer = FakeScorer(score_sequence=[0.1, 0.9])
    detector = OpenWakeWordDetector(
        threshold=0.5, pre_roll_ms=300, scorer=scorer
    )
    first = _pcm(2000, 44)
    second = _pcm(2000, 55)

    assert detector.process(first) is None
    detection = detector.process(second)

    assert detection is not None
    assert detection.pre_roll == first + second


def test_a_zero_pre_roll_yields_an_empty_window() -> None:
    scorer = FakeScorer({"athena": 0.9})
    detector = OpenWakeWordDetector(
        threshold=0.5, pre_roll_ms=0, scorer=scorer
    )

    detection = detector.process(_pcm(1024))

    assert detection is not None
    assert detection.pre_roll == b""


def test_a_score_equal_to_the_threshold_triggers() -> None:
    detector = OpenWakeWordDetector(
        threshold=0.5, scorer=FakeScorer({"athena": 0.5})
    )

    assert detector.process(_pcm(1024)) == Detection(score=0.5, pre_roll=_pcm(1024))


def test_the_model_sidecar_threshold_takes_precedence_over_the_configured_one(
    tmp_path,
) -> None:
    model = tmp_path / "athena.onnx"
    model.write_bytes(b"placeholder")
    model.with_suffix(".json").write_text('{"threshold": 0.75}', encoding="utf-8")
    scorer = FakeScorer({"athena": 0.7})
    detector = OpenWakeWordDetector(model_path=model, threshold=0.5, scorer=scorer)

    assert detector.process(_pcm(1024)) is None

    scorer.scores = {"athena": 0.8}
    assert detector.process(_pcm(1024)) is not None


def test_without_a_sidecar_the_configured_threshold_applies(tmp_path) -> None:
    model = tmp_path / "athena.onnx"
    model.write_bytes(b"placeholder")
    detector = OpenWakeWordDetector(
        model_path=model, threshold=0.6, scorer=FakeScorer({"athena": 0.55})
    )

    assert detector.process(_pcm(1024)) is None


def test_a_sidecar_without_a_threshold_keeps_the_configured_one(tmp_path) -> None:
    model = tmp_path / "athena.onnx"
    model.write_bytes(b"placeholder")
    model.with_suffix(".json").write_text('{"name": "athena"}', encoding="utf-8")
    detector = OpenWakeWordDetector(
        model_path=model, threshold=0.6, scorer=FakeScorer({"athena": 0.65})
    )

    assert detector.process(_pcm(1024)) is not None


def test_a_malformed_sidecar_fails_with_a_clear_error(tmp_path) -> None:
    model = tmp_path / "athena.onnx"
    model.write_bytes(b"placeholder")
    sidecar = model.with_suffix(".json")
    sidecar.write_text("{not json", encoding="utf-8")

    with pytest.raises(WakeWordUnavailableError, match="athena.json"):
        OpenWakeWordDetector(
            model_path=model, threshold=0.5, scorer=FakeScorer()
        )


def test_the_vad_is_disabled_by_default_and_not_consulted() -> None:
    vad = FakeVad(speech=0.0)
    detector = OpenWakeWordDetector(
        threshold=0.5, scorer=FakeScorer({"athena": 0.9}), vad_model=vad
    )

    detection = detector.process(_pcm(1024))

    assert detection is not None
    assert vad.chunks == []


def test_the_vad_gate_accepts_a_speech_frame() -> None:
    vad = FakeVad(speech=0.9)
    detector = OpenWakeWordDetector(
        threshold=0.5,
        scorer=FakeScorer({"athena": 0.9}),
        vad=True,
        vad_model=vad,
    )
    chunk = _pcm(1024)

    detection = detector.process(chunk)

    assert detection is not None
    assert len(vad.chunks) == 1
    assert vad.chunks[0].dtype == np.int16
    assert vad.chunks[0].tobytes() == chunk


def test_the_vad_gate_rejects_a_non_speech_frame() -> None:
    vad = FakeVad(speech=0.1)
    detector = OpenWakeWordDetector(
        threshold=0.5,
        scorer=FakeScorer({"athena": 0.9}),
        vad=True,
        vad_model=vad,
    )

    assert detector.process(_pcm(1024)) is None
    assert len(vad.chunks) == 1


def test_the_vad_threshold_is_configurable() -> None:
    detector = OpenWakeWordDetector(
        threshold=0.5,
        scorer=FakeScorer({"athena": 0.9}),
        vad=True,
        vad_model=FakeVad(speech=0.7),
        vad_threshold=0.8,
    )

    assert detector.process(_pcm(1024)) is None


def test_a_missing_vad_model_fails_by_its_explicit_path(tmp_path) -> None:
    missing = tmp_path / "silero_vad.onnx"

    with pytest.raises(WakeWordUnavailableError, match="silero_vad.onnx"):
        OpenWakeWordDetector(
            threshold=0.5,
            scorer=FakeScorer(),
            vad=True,
            vad_model_path=missing,
        )


class RaisingScorer(FakeScorer):
    def predict(self, samples: np.ndarray) -> dict[str, float]:
        raise RuntimeError("inférence cassée")


class RaisingVad(FakeVad):
    def predict(self, samples: np.ndarray) -> float:
        raise RuntimeError("VAD cassé")


def test_inference_errors_are_not_swallowed() -> None:
    detector = OpenWakeWordDetector(threshold=0.5, scorer=RaisingScorer())

    with pytest.raises(RuntimeError, match="inférence cassée"):
        detector.process(_pcm(1024))


def test_vad_errors_are_not_swallowed() -> None:
    detector = OpenWakeWordDetector(
        threshold=0.5,
        scorer=FakeScorer({"athena": 0.9}),
        vad=True,
        vad_model=RaisingVad(),
    )

    with pytest.raises(RuntimeError, match="VAD cassé"):
        detector.process(_pcm(1024))


def test_debug_scores_are_logged_but_throttled(caplog) -> None:
    detector = OpenWakeWordDetector(
        threshold=0.5,
        scorer=FakeScorer({"athena": 0.1}),
        score_log_interval=2,
    )

    with caplog.at_level(logging.DEBUG, logger="athena.client"):
        for _ in range(5):
            assert detector.process(_pcm(1024)) is None

    scores = [
        record.message
        for record in caplog.records
        if "Score de Wake word" in record.message
    ]
    assert len(scores) == 2


def test_close_releases_the_scorer_and_the_vad_exactly_once() -> None:
    scorer = FakeScorer()
    vad = FakeVad()
    detector = OpenWakeWordDetector(
        threshold=0.5, scorer=scorer, vad=True, vad_model=vad
    )

    detector.close()
    detector.close()

    assert scorer.resets == 1
    assert scorer.closes == 1
    assert vad.closes == 1


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
