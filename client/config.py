from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field


def _get_env(key: str, default: str) -> str:
    return os.getenv(key, default)


@dataclass
class AudioConfig:
    channels: int = 1
    send_sample_rate: int = 16000
    receive_sample_rate: int = 24000
    chunk_size: int = 1024


@dataclass
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


@dataclass
class ClientConfig:
    server_url: str = field(
        default_factory=lambda: _get_env("WS_SERVER_URL", "ws://localhost:8765")
    )
    client_name: str = field(
        default_factory=lambda: _get_env("CLIENT_NAME", "desktop")
    )
    platform: str = "desktop"
    ping_interval: float = 30.0
    reconnect: BackoffConfig = field(default_factory=BackoffConfig)


@dataclass
class AppConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    client: ClientConfig = field(default_factory=ClientConfig)
    log_level: str = field(default_factory=lambda: _get_env("LOG_LEVEL", "INFO"))
