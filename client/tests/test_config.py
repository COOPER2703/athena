from __future__ import annotations

from client.config import AppConfig, AudioConfig, BackoffConfig, ClientConfig


def test_backoff_delays_grow_exponentially_then_cap() -> None:
    delays = BackoffConfig(initial=0.5, maximum=4.0, factor=2.0).delays()
    assert [next(delays) for _ in range(5)] == [0.5, 1.0, 2.0, 4.0, 4.0]


def test_client_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("WS_SERVER_URL", raising=False)
    monkeypatch.delenv("CLIENT_NAME", raising=False)
    cfg = ClientConfig()
    assert cfg.server_url == "ws://localhost:8765"
    assert cfg.client_name == "desktop"
    assert cfg.platform == "desktop"


def test_client_config_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("WS_SERVER_URL", "ws://example:1234")
    monkeypatch.setenv("CLIENT_NAME", "laptop")
    cfg = ClientConfig()
    assert cfg.server_url == "ws://example:1234"
    assert cfg.client_name == "laptop"


def test_audio_config_uses_pcm16_mono_rates() -> None:
    cfg = AudioConfig()
    assert cfg.channels == 1
    assert cfg.send_sample_rate == 16000
    assert cfg.receive_sample_rate == 24000


def test_app_config_composes_audio_and_client() -> None:
    cfg = AppConfig()
    assert isinstance(cfg.audio, AudioConfig)
    assert isinstance(cfg.client, ClientConfig)
