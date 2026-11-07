from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from client.config import DEFAULT_WAKE_WORD_MODEL

log = logging.getLogger("athena.client")

_MODEL_DIR = Path(__file__).resolve().parent / "models"
DEFAULT_MELSPEC_MODEL = _MODEL_DIR / "melspectrogram.onnx"
DEFAULT_EMBEDDING_MODEL = _MODEL_DIR / "embedding_model.onnx"


class WakeWordUnavailableError(RuntimeError):
    """Le modèle, ses features ou le runtime openWakeWord sont introuvables."""


@dataclass(frozen=True, slots=True)
class Detection:
    """Un déclenchement du Wake word, avec le score du classifieur."""

    score: float


class Scorer(Protocol):
    """Interface minimale du classifieur openWakeWord, injectable en test."""

    def predict(self, samples: np.ndarray) -> Mapping[str, float]: ...

    def reset(self) -> None: ...


class WakeWordDetector(Protocol):
    """Port de la Veille consommé par l'orchestrateur du Client."""

    def process(self, chunk: bytes) -> Detection | None: ...

    def close(self) -> None: ...


class OpenWakeWordDetector:
    """Détecteur local adossé à un classifieur openWakeWord ONNX.

    Les trois modèles (classifieur Athena, melspectrogram, embedding) sont
    chargés par chemins explicites : rien n'est téléchargé au démarrage et le
    paquet installé n'est pas requis pour retrouver ses ressources.
    """

    def __init__(
        self,
        model_path: str | Path = DEFAULT_WAKE_WORD_MODEL,
        *,
        threshold: float = 0.5,
        melspec_model_path: str | Path = DEFAULT_MELSPEC_MODEL,
        embedding_model_path: str | Path = DEFAULT_EMBEDDING_MODEL,
        scorer: Scorer | None = None,
    ) -> None:
        self._threshold = threshold
        self._closed = False

        if scorer is not None:
            self._scorer = scorer
            return

        model = Path(model_path)
        melspec = Path(melspec_model_path)
        embedding = Path(embedding_model_path)
        missing = [path for path in (model, melspec, embedding) if not path.is_file()]
        if missing:
            raise WakeWordUnavailableError(
                "Modèle de Wake word ou modèles de features introuvables : "
                + ", ".join(str(path) for path in missing)
                + ". Vérifiez ATHENA_WAKE_WORD_MODEL et l'installation du paquet."
            )

        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise WakeWordUnavailableError(
                "openWakeWord est introuvable : réinstallez le Client avec ses "
                "dépendances (openwakeword, onnxruntime)."
            ) from exc

        self._scorer = Model(
            wakeword_models=[str(model)],
            inference_framework="onnx",
            melspec_model_path=str(melspec),
            embedding_model_path=str(embedding),
        )
        log.info(
            "Modèle de Wake word chargé (modèle=%s, seuil=%.2f)", model, threshold
        )

    def process(self, chunk: bytes) -> Detection | None:
        if self._closed or not chunk:
            return None
        samples = np.frombuffer(chunk, dtype=np.int16)
        predictions = self._scorer.predict(samples)
        if not predictions:
            return None
        score = float(max(predictions.values()))
        if score < self._threshold:
            return None
        return Detection(score=score)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._scorer.reset()
