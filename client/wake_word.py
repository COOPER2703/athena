from __future__ import annotations

import json
import logging
import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from client.config import DEFAULT_WAKE_WORD_MODEL, DEFAULT_WAKE_WORD_VAD_MODEL

log = logging.getLogger("athena.client")

_MODEL_DIR = Path(__file__).resolve().parent / "models"
DEFAULT_MELSPEC_MODEL = _MODEL_DIR / "melspectrogram.onnx"
DEFAULT_EMBEDDING_MODEL = _MODEL_DIR / "embedding_model.onnx"
DEFAULT_VAD_MODEL = DEFAULT_WAKE_WORD_VAD_MODEL

# Seuil de parole du gate VAD : en dessous, un score de Wake word est ignoré.
DEFAULT_VAD_THRESHOLD = 0.5


class WakeWordUnavailableError(RuntimeError):
    """Le modèle, ses features ou le runtime openWakeWord sont introuvables."""


@dataclass(frozen=True, slots=True)
class Detection:
    """Un déclenchement du Wake word.

    ``pre_roll`` contient les dernières ``pre_roll_ms`` millisecondes de PCM16
    mono, chunk courant inclus, pour ne pas perdre la première syllabe de la
    commande.
    """

    score: float
    pre_roll: bytes = b""


class _PreRollBuffer:
    """Fenêtre glissante bornée de PCM16, indépendante de la taille des chunks."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max(0, max_bytes)
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def push(self, chunk: bytes) -> None:
        if not self._max_bytes or not chunk:
            return
        if len(chunk) >= self._max_bytes:
            self._chunks.clear()
            self._chunks.append(chunk[-self._max_bytes :])
            self._size = self._max_bytes
            return
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size > self._max_bytes:
            excess = self._size - self._max_bytes
            head = self._chunks[0]
            if len(head) <= excess:
                self._chunks.popleft()
                self._size -= len(head)
            else:
                self._chunks[0] = head[excess:]
                self._size -= excess

    def snapshot(self) -> bytes:
        return b"".join(self._chunks)


class Scorer(Protocol):
    """Interface minimale du classifieur openWakeWord, injectable en test."""

    def predict(self, samples: np.ndarray) -> Mapping[str, float]: ...

    def reset(self) -> None: ...


class SpeechDetector(Protocol):
    """Interface minimale du VAD, injectable en test."""

    def predict(self, samples: np.ndarray) -> float: ...


class SileroVad:
    """Gate VAD adossé à Silero, chargé par un chemin explicite.

    Le ``vad_threshold`` intégré d'openWakeWord n'est pas utilisé : il charge
    ``silero_vad.onnx`` depuis les ressources du paquet installé, ce qui rend le
    fonctionnement hors ligne dépendant de l'installation. Ici le fichier
    versionné est passé tel quel et les trames sont découpées nous-mêmes pour
    accepter des chunks de taille quelconque.
    """

    def __init__(
        self,
        model_path: str | Path = DEFAULT_VAD_MODEL,
        *,
        frame_size: int = 480,
    ) -> None:
        path = Path(model_path)
        if not path.is_file():
            raise WakeWordUnavailableError(
                f"Modèle VAD introuvable : {path}. Vérifiez ATHENA_WAKE_WORD_VAD_MODEL "
                "et l'installation du paquet."
            )
        try:
            from openwakeword.vad import VAD
        except ImportError as exc:
            raise WakeWordUnavailableError(
                "openWakeWord est introuvable : le gate VAD exige ses dépendances "
                "(openwakeword, onnxruntime)."
            ) from exc

        self._model = VAD(model_path=str(path))
        self._frame_size = frame_size
        self._pending = np.empty(0, dtype=np.int16)
        self._closed = False

    def predict(self, samples: np.ndarray) -> float:
        if self._closed or samples.size == 0:
            return 0.0
        data = (
            np.concatenate((self._pending, samples))
            if self._pending.size
            else samples
        )
        frame_size = self._frame_size
        complete = data.size // frame_size
        if complete == 0:
            self._pending = data.copy()
            return 0.0

        score = 0.0
        for index in range(complete):
            frame = data[index * frame_size : (index + 1) * frame_size]
            score = max(
                score,
                float(self._model.predict(frame, frame_size=frame_size)),
            )
        self._pending = data[complete * frame_size :].copy()
        return score

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        model, self._model = self._model, None
        self._pending = np.empty(0, dtype=np.int16)
        reset_states = getattr(model, "reset_states", None)
        if callable(reset_states):
            reset_states()


class WakeWordDetector(Protocol):
    """Port de la Veille consommé par l'orchestrateur du Client."""

    def process(self, chunk: bytes) -> Detection | None: ...

    def close(self) -> None: ...


def _read_model_threshold(model_path: Path) -> float | None:
    """Lit ``threshold`` dans le ``.json`` posé à côté du modèle, s'il existe."""
    sidecar = model_path.with_suffix(".json")
    if not sidecar.is_file():
        return None
    try:
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WakeWordUnavailableError(
            f"Métadonnées du modèle de Wake word illisibles : {sidecar}."
        ) from exc
    if not isinstance(metadata, dict):
        raise WakeWordUnavailableError(
            f"Métadonnées du modèle de Wake word invalides : {sidecar}."
        )
    raw_threshold = metadata.get("threshold")
    if raw_threshold is None:
        return None
    try:
        threshold = float(raw_threshold)
    except (TypeError, ValueError) as exc:
        raise WakeWordUnavailableError(
            f"Seuil invalide dans les métadonnées du modèle : {sidecar}."
        ) from exc
    if not math.isfinite(threshold):
        raise WakeWordUnavailableError(
            f"Seuil invalide dans les métadonnées du modèle : {sidecar}."
        )
    return threshold


class OpenWakeWordDetector:
    """Détecteur local adossé à un classifieur openWakeWord ONNX.

    Les trois modèles (classifieur Athena, melspectrogram, embedding) sont
    chargés par chemins explicites : rien n'est téléchargé au démarrage et le
    paquet installé n'est pas requis pour retrouver ses ressources.

    Le seuil effectif est celui du fichier ``<modèle>.json`` quand il définit
    ``threshold`` ; sinon celui fourni à la construction (issu de
    ``WakeWordConfig.threshold``).
    """

    def __init__(
        self,
        model_path: str | Path = DEFAULT_WAKE_WORD_MODEL,
        *,
        threshold: float = 0.5,
        melspec_model_path: str | Path = DEFAULT_MELSPEC_MODEL,
        embedding_model_path: str | Path = DEFAULT_EMBEDDING_MODEL,
        pre_roll_ms: int = 300,
        sample_rate: int = 16_000,
        vad: bool = False,
        vad_model_path: str | Path = DEFAULT_VAD_MODEL,
        vad_threshold: float = DEFAULT_VAD_THRESHOLD,
        score_log_interval: int = 10,
        scorer: Scorer | None = None,
        vad_model: SpeechDetector | None = None,
    ) -> None:
        model = Path(model_path)
        model_threshold = _read_model_threshold(model)
        self._threshold = (
            threshold if model_threshold is None else model_threshold
        )
        self._closed = False
        self._scorer: Scorer | None = None
        self._pre_roll = _PreRollBuffer(
            sample_rate * 2 * max(0, pre_roll_ms) // 1000
        )
        self._vad_threshold = vad_threshold
        self._vad: SpeechDetector | None = None
        self._score_log_interval = max(1, score_log_interval)
        self._frames = 0
        if vad:
            self._vad = (
                SileroVad(vad_model_path) if vad_model is None else vad_model
            )
            log.info(
                "Gate VAD activé (modèle=%s, seuil=%.2f)",
                vad_model_path if vad_model is None else "injecté",
                vad_threshold,
            )

        if scorer is not None:
            self._scorer = scorer
            return

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
            "Modèle de Wake word chargé (modèle=%s, seuil=%.2f)",
            model,
            self._threshold,
        )

    def process(self, chunk: bytes) -> Detection | None:
        if self._closed or self._scorer is None or not chunk:
            return None
        self._pre_roll.push(chunk)
        samples = np.frombuffer(chunk, dtype=np.int16)
        predictions = self._scorer.predict(samples)
        if not predictions:
            return None
        score = float(max(predictions.values()))
        self._frames += 1
        if self._frames % self._score_log_interval == 0:
            log.debug(
                "Score de Wake word : %.3f (seuil=%.2f)", score, self._threshold
            )
        if score < self._threshold:
            return None
        if (
            self._vad is not None
            and self._vad.predict(samples) < self._vad_threshold
        ):
            return None
        return Detection(score=score, pre_roll=self._pre_roll.snapshot())

    def close(self) -> None:
        """Libère le classifieur et le VAD ; appelable plusieurs fois."""
        if self._closed:
            return
        self._closed = True
        scorer, self._scorer = self._scorer, None
        vad, self._vad = self._vad, None
        for resource in (scorer, vad):
            if resource is None:
                continue
            reset = getattr(resource, "reset", None)
            if callable(reset):
                reset()
            close = getattr(resource, "close", None)
            if callable(close):
                close()
