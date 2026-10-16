from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from server.app.config import (
    DEFAULT_CONSOLIDATION_MODEL,
    DEFAULT_EMBED_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_VOICE,
    DEFAULT_MEMORY_DB,
    AppConfig,
    LogLevel,
    MemoryConfig,
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


def test_memory_defaults_are_wired_on_the_app_config() -> None:
    cfg = load_config(env={"GEMINI_API_KEY": "k"}, env_file=None)
    assert cfg.memory == MemoryConfig()
    assert cfg.memory.user_id == "owner"
    assert cfg.memory.db_path == DEFAULT_MEMORY_DB
    assert cfg.memory.short_term_window == 20
    assert cfg.memory.recent_events_window == 20
    assert cfg.memory.recall_k == 5
    assert cfg.memory.recall_window_turns == 3
    assert cfg.memory.recall_max_distance == 0.6
    assert cfg.memory.dedup_threshold == 0.15
    assert cfg.memory.journal_retention_days == 3
    assert cfg.memory.embed_model == DEFAULT_EMBED_MODEL
    assert cfg.memory.consolidation_model == DEFAULT_CONSOLIDATION_MODEL
    assert cfg.memory.consolidation_hours == (0, 12)


def test_memory_reads_and_coerces_environment() -> None:
    cfg = load_config(
        env={
            "GEMINI_API_KEY": "k",
            "ATHENA_USER_ID": "alice",
            "ATHENA_MEMORY_DB": "/tmp/mem.db",
            "ATHENA_SHORT_TERM_WINDOW": "7",
            "ATHENA_RECENT_EVENTS_WINDOW": "9",
            "ATHENA_RECALL_K": "4",
            "ATHENA_RECALL_WINDOW_TURNS": "2",
            "ATHENA_RECALL_MAX_DISTANCE": "0.5",
            "ATHENA_DEDUP_THRESHOLD": "0.2",
            "ATHENA_JOURNAL_RETENTION_DAYS": "6",
            "ATHENA_EMBED_MODEL": "custom-embed",
            "ATHENA_CONSOLIDATION_MODEL": "custom-consolidation",
            "ATHENA_CONSOLIDATION_HOURS": "6,18",
        },
        env_file=None,
    )
    assert cfg.memory.user_id == "alice"
    assert cfg.memory.db_path == "/tmp/mem.db"
    assert cfg.memory.short_term_window == 7
    assert cfg.memory.recent_events_window == 9
    assert cfg.memory.recall_k == 4
    assert cfg.memory.recall_window_turns == 2
    assert cfg.memory.recall_max_distance == 0.5
    assert cfg.memory.dedup_threshold == 0.2
    assert cfg.memory.journal_retention_days == 6
    assert cfg.memory.embed_model == "custom-embed"
    assert cfg.memory.consolidation_model == "custom-consolidation"
    assert cfg.memory.consolidation_hours == (6, 18)


def test_cosine_distance_thresholds_allow_values_above_one() -> None:
    # ``vec_distance_cosine`` ranges over [0, 2]; both thresholds are upper
    # bounds on that distance, so a legitimate value in (1, 2] must be accepted.
    cfg = load_config(
        env={
            "GEMINI_API_KEY": "k",
            "ATHENA_RECALL_MAX_DISTANCE": "1.5",
            "ATHENA_DEDUP_THRESHOLD": "1.9",
        },
        env_file=None,
    )
    assert cfg.memory.recall_max_distance == 1.5
    assert cfg.memory.dedup_threshold == 1.9


@pytest.mark.parametrize(
    "overrides",
    [
        {"ATHENA_USER_ID": ""},
        {"ATHENA_USER_ID": "   "},
        {"ATHENA_MEMORY_DB": ""},
        {"ATHENA_SHORT_TERM_WINDOW": "0"},
        {"ATHENA_RECENT_EVENTS_WINDOW": "-1"},
        {"ATHENA_JOURNAL_RETENTION_DAYS": "0"},
        {"ATHENA_RECALL_K": "0"},
        {"ATHENA_RECALL_WINDOW_TURNS": "0"},
        {"ATHENA_RECALL_MAX_DISTANCE": "2.5"},
        {"ATHENA_DEDUP_THRESHOLD": "2.5"},
        {"ATHENA_DEDUP_THRESHOLD": "-0.1"},
        {"ATHENA_SHORT_TERM_WINDOW": "not-a-number"},
        {"ATHENA_CONSOLIDATION_HOURS": "24"},
    ],
)
def test_invalid_memory_config_raises_clear_validation_error(overrides) -> None:
    with pytest.raises(ValidationError):
        load_config(env={"GEMINI_API_KEY": "k", **overrides}, env_file=None)


def test_memory_config_is_immutable() -> None:
    cfg = load_config(env={"GEMINI_API_KEY": "k"}, env_file=None)
    with pytest.raises(ValidationError):
        cfg.memory.user_id = "other"  # type: ignore[misc]
