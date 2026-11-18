from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from client.config import (
    AppConfig,
    AudioConfig,
    BackoffConfig,
    ClientConfig,
    PageConfig,
    WakeWordConfig,
)


def test_backoff_delays_grow_exponentially_then_cap() -> None:
    delays = BackoffConfig(initial=0.5, maximum=4.0, factor=2.0).delays()
    assert [next(delays) for _ in range(5)] == [0.5, 1.0, 2.0, 4.0, 4.0]


def test_client_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WS_SERVER_URL", raising=False)
    monkeypatch.delenv("CLIENT_NAME", raising=False)
    monkeypatch.delenv("ATHENA_ACCESS_TOKEN", raising=False)
    cfg = ClientConfig()
    assert cfg.server_url == "ws://localhost:8765"
    assert cfg.client_name == "desktop"
    assert cfg.platform == "desktop"
    assert cfg.access_token == ""


def test_client_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WS_SERVER_URL", "ws://example:1234")
    monkeypatch.setenv("CLIENT_NAME", "laptop")
    cfg = ClientConfig()
    assert cfg.server_url == "ws://example:1234"
    assert cfg.client_name == "laptop"


def test_client_config_reads_the_access_token_from_its_dedicated_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_ACCESS_TOKEN", "ath_secret")
    assert ClientConfig().access_token == "ath_secret"


def test_audio_config_uses_pcm16_mono_rates() -> None:
    cfg = AudioConfig()
    assert cfg.channels == 1
    assert cfg.send_sample_rate == 16000
    assert cfg.receive_sample_rate == 24000


WAKE_WORD_ENV = (
    "ATHENA_WAKE_WORD",
    "ATHENA_WAKE_WORD_MODEL",
    "ATHENA_WAKE_WORD_THRESHOLD",
    "ATHENA_WAKE_WORD_COOLDOWN_SECONDS",
    "ATHENA_WAKE_WORD_VAD",
    "ATHENA_WAKE_WORD_VAD_MODEL",
    "ATHENA_WAKE_WORD_SESSION_START_TIMEOUT_SECONDS",
    "ATHENA_WAKE_WORD_PRE_ROLL_MS",
)


def _clear_wake_word_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in WAKE_WORD_ENV:
        monkeypatch.delenv(key, raising=False)


def test_wake_word_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_wake_word_env(monkeypatch)
    cfg = WakeWordConfig()
    assert cfg.enabled is True
    assert cfg.model_path.parts[-3:] == ("client", "models", "athena.onnx")
    assert cfg.threshold == 0.5
    assert cfg.cooldown_seconds == 2.0
    assert cfg.vad is False
    assert cfg.vad_model_path.parts[-3:] == (
        "client",
        "models",
        "silero_vad.onnx",
    )
    assert cfg.session_start_timeout_seconds == 10.0
    assert cfg.pre_roll_ms == 300


def test_wake_word_config_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_wake_word_env(monkeypatch)
    monkeypatch.setenv("ATHENA_WAKE_WORD", "0")
    assert WakeWordConfig().enabled is False


def test_wake_word_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_wake_word_env(monkeypatch)
    monkeypatch.setenv("ATHENA_WAKE_WORD", "1")
    monkeypatch.setenv("ATHENA_WAKE_WORD_MODEL", "/tmp/custom/athena.onnx")
    monkeypatch.setenv("ATHENA_WAKE_WORD_THRESHOLD", "0.7")
    monkeypatch.setenv("ATHENA_WAKE_WORD_COOLDOWN_SECONDS", "4.5")
    monkeypatch.setenv("ATHENA_WAKE_WORD_VAD", "1")
    monkeypatch.setenv("ATHENA_WAKE_WORD_VAD_MODEL", "/tmp/custom/silero_vad.onnx")
    monkeypatch.setenv("ATHENA_WAKE_WORD_SESSION_START_TIMEOUT_SECONDS", "3.0")
    monkeypatch.setenv("ATHENA_WAKE_WORD_PRE_ROLL_MS", "500")
    cfg = WakeWordConfig()
    assert cfg.enabled is True
    assert cfg.model_path == Path("/tmp/custom/athena.onnx")
    assert cfg.threshold == 0.7
    assert cfg.cooldown_seconds == 4.5
    assert cfg.vad is True
    assert cfg.vad_model_path == Path("/tmp/custom/silero_vad.onnx")
    assert cfg.session_start_timeout_seconds == 3.0
    assert cfg.pre_roll_ms == 500


PAGE_ENV = (
    "ATHENA_CLIENT_HOST",
    "ATHENA_CLIENT_PORT",
    "ATHENA_CLIENT_CONFIG",
    "XDG_CONFIG_HOME",
)


def test_page_config_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in PAGE_ENV:
        monkeypatch.delenv(key, raising=False)
    cfg = PageConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8768
    assert cfg.config_path.parts[-3:] == (".config", "athena", "client.json")


def test_page_config_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENA_CLIENT_HOST", "localhost")
    monkeypatch.setenv("ATHENA_CLIENT_PORT", "9100")
    monkeypatch.setenv("ATHENA_CLIENT_CONFIG", "/tmp/custom/client.json")
    cfg = PageConfig()
    assert cfg.host == "localhost"
    assert cfg.port == 9100
    assert cfg.config_path == Path("/tmp/custom/client.json")


def test_page_config_honours_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in PAGE_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert PageConfig().config_path == Path("/tmp/xdg/athena/client.json")


def test_app_config_composes_audio_and_client() -> None:
    cfg = AppConfig()
    assert isinstance(cfg.audio, AudioConfig)
    assert isinstance(cfg.client, ClientConfig)
    assert isinstance(cfg.wake_word, WakeWordConfig)
    assert isinstance(cfg.page, PageConfig)


def test_config_value_objects_are_frozen() -> None:
    cfg = ClientConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(cfg, "platform", "mobile")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(WakeWordConfig(), "threshold", 0.9)
