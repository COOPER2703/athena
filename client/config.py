from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path


def _get_env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _get_path(key: str, default: Path) -> Path:
    return Path(_get_env(key, str(default))).expanduser()


def _get_float(key: str, default: float) -> float:
    return float(_get_env(key, str(default)))


def _get_int(key: str, default: int) -> int:
    return int(_get_env(key, str(default)))


def _get_flag(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


# Variable d'environnement dédiée au Jeton d'accès du Client desktop. Le Jeton
# est un credential machine : il ne passe ni par le Protocole (figé), ni par une
# URL, mais par l'en-tête ``Authorization: Bearer`` du handshake.
ACCESS_TOKEN_ENV = "ATHENA_ACCESS_TOKEN"

# Les assets de la Veille sont versionnés dans le paquet : le défaut reste
# valable depuis un checkout comme depuis un wheel installé, quel que soit le
# répertoire de travail.
_CLIENT_DIR = Path(__file__).resolve().parent
DEFAULT_WAKE_WORD_MODEL = _CLIENT_DIR / "models" / "athena.onnx"


@dataclass(frozen=True, slots=True)
class AudioConfig:
    channels: int = 1
    send_sample_rate: int = 16000
    receive_sample_rate: int = 24000
    chunk_size: int = 1024


@dataclass(frozen=True, slots=True)
class BackoffConfig:
    """Paramètres du backoff exponentiel de reconnexion."""

    initial: float = 1.0
    maximum: float = 30.0
    factor: float = 2.0

    def delays(self) -> Iterator[float]:
        """Suite infinie des délais, plafonnée à ``maximum``."""
        delay = self.initial
        while True:
            yield delay
            delay = min(delay * self.factor, self.maximum)


@dataclass(frozen=True, slots=True)
class ClientConfig:
    server_url: str = field(
        default_factory=lambda: _get_env("WS_SERVER_URL", "ws://localhost:8765")
    )
    client_name: str = field(
        default_factory=lambda: _get_env("CLIENT_NAME", "desktop")
    )
    # Aucun défaut secret : sans Jeton, le Client refuse de démarrer plutôt que
    # de tenter un handshake qui sera refusé.
    access_token: str = field(
        default_factory=lambda: _get_env(ACCESS_TOKEN_ENV, "")
    )
    platform: str = "desktop"
    ping_interval: float = 30.0
    reconnect: BackoffConfig = field(default_factory=BackoffConfig)


@dataclass(frozen=True, slots=True)
class WakeWordConfig:
    """Paramètres de la Veille locale.

    ``ATHENA_WAKE_WORD=0`` coupe la détection et laisse le push-to-talk ; les
    autres variables surchargent les valeurs par défaut de la spec.
    """

    enabled: bool = field(default_factory=lambda: _get_flag("ATHENA_WAKE_WORD", True))
    model_path: Path = field(
        default_factory=lambda: _get_path(
            "ATHENA_WAKE_WORD_MODEL", DEFAULT_WAKE_WORD_MODEL
        )
    )
    threshold: float = field(
        default_factory=lambda: _get_float("ATHENA_WAKE_WORD_THRESHOLD", 0.5)
    )
    cooldown_seconds: float = field(
        default_factory=lambda: _get_float(
            "ATHENA_WAKE_WORD_COOLDOWN_SECONDS", 2.0
        )
    )
    vad: bool = field(default_factory=lambda: _get_flag("ATHENA_WAKE_WORD_VAD", False))
    session_start_timeout_seconds: float = field(
        default_factory=lambda: _get_float(
            "ATHENA_WAKE_WORD_SESSION_START_TIMEOUT_SECONDS", 10.0
        )
    )
    pre_roll_ms: int = field(
        default_factory=lambda: _get_int("ATHENA_WAKE_WORD_PRE_ROLL_MS", 300)
    )


@dataclass(frozen=True, slots=True)
class AppConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    client: ClientConfig = field(default_factory=ClientConfig)
    wake_word: WakeWordConfig = field(default_factory=WakeWordConfig)
    log_level: str = field(default_factory=lambda: _get_env("LOG_LEVEL", "INFO"))
