from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from server.app.config import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_VOICE,
    AppConfig,
    LogLevel,
    ServerConfig,
    load_config,
)
from server.app.secrets import MissingSecretError


def test_defaults_with_only_required_secret() -> None:
    cfg = load_config(env={"GEMINI_API_KEY": "k"}, env_file=None)
    assert isinstance(cfg, AppConfig)
    assert cfg.server == ServerConfig(host="0.0.0.0", port=8765)
    assert cfg.log_level is LogLevel.INFO
    assert cfg.debug_decisions is False
    assert cfg.gemini.api_key == "k"
    assert cfg.gemini.model == DEFAULT_GEMINI_MODEL
    assert cfg.gemini.voice == DEFAULT_GEMINI_VOICE


def test_reads_and_coerces_environment() -> None:
    cfg = load_config(
        env={
            "GEMINI_API_KEY": "k",
            "WS_HOST": "127.0.0.1",
            "WS_PORT": "9000",
            "LOG_LEVEL": "debug",
            "ATHENA_DEBUG_DECISIONS": "true",
            "GEMINI_MODEL": "custom-model",
            "GEMINI_VOICE": "Puck",
        },
        env_file=None,
    )
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 9000
    assert cfg.log_level is LogLevel.DEBUG
    assert cfg.debug_decisions is True
    assert cfg.gemini.model == "custom-model"
    assert cfg.gemini.voice == "Puck"


def test_missing_api_key_raises_clear_startup_error() -> None:
    with pytest.raises(MissingSecretError) as excinfo:
        load_config(env={}, env_file=None)
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_invalid_port_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        load_config(env={"GEMINI_API_KEY": "k", "WS_PORT": "not-a-port"}, env_file=None)


def test_unknown_log_level_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        load_config(env={"GEMINI_API_KEY": "k", "LOG_LEVEL": "shouty"}, env_file=None)


def test_debug_decisions_defaults_off_when_unset() -> None:
    cfg = load_config(env={"GEMINI_API_KEY": "k"}, env_file=None)
    assert cfg.debug_decisions is False


def test_loads_values_from_dotenv_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "WS_PORT=9100\n"
        "GEMINI_API_KEY=from-file\n"
        "GEMINI_VOICE=Puck\n"
        "ATHENA_DEBUG_DECISIONS=1\n",
        encoding="utf-8",
    )
    cfg = load_config(env={}, env_file=env_file)
    assert cfg.server.port == 9100
    assert cfg.gemini.api_key == "from-file"
    assert cfg.gemini.voice == "Puck"
    assert cfg.debug_decisions is True


def test_real_environment_overrides_dotenv(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("WS_PORT=9100\nGEMINI_API_KEY=from-file\n", encoding="utf-8")
    cfg = load_config(env={"WS_PORT": "9200", "GEMINI_API_KEY": "from-env"}, env_file=env_file)
    assert cfg.server.port == 9200
    assert cfg.gemini.api_key == "from-env"


def test_secret_is_resolved_through_injected_resolver() -> None:
    class FakeResolver:
        def resolve(self, name: str) -> str:
            return f"resolved:{name}"

    cfg = load_config(env={}, env_file=None, secrets=FakeResolver())
    assert cfg.gemini.api_key == "resolved:GEMINI_API_KEY"


def test_config_is_immutable() -> None:
    cfg = load_config(env={"GEMINI_API_KEY": "k"}, env_file=None)
    with pytest.raises(ValidationError):
        cfg.log_level = LogLevel.DEBUG  # type: ignore[misc]
