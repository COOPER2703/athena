from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from server.app.config import (
    DEFAULT_CONSOLIDATION_MODEL,
    DEFAULT_CONSOLE_HOST,
    DEFAULT_CONSOLE_PORT,
    DEFAULT_CONSOLE_STATIC_DIR,
    DEFAULT_CONSOLE_TRACE_BUFFER,
    DEFAULT_EMBED_MODEL,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_VOICE,
    DEFAULT_JUDGE_MODEL,
    DEFAULT_MEMORY_DB,
    DEFAULT_WEBHOOK_HOST,
    DEFAULT_WEBHOOK_PORT,
    AppConfig,
    ConsoleConfig,
    LogLevel,
    MemoryConfig,
    PluginsConfig,
    ServerConfig,
    WebhookConfig,
    load_config,
)
from server.app.secrets import MissingSecretError


def test_defaults_with_only_required_secret() -> None:
    cfg = load_config(env={"GEMINI_API_KEY": "k"}, env_file=None)
    assert isinstance(cfg, AppConfig)
    assert cfg.server == ServerConfig(host="0.0.0.0", port=8765)
    assert cfg.log_level is LogLevel.INFO
    assert cfg.log_dir == "logs"
    assert cfg.debug_decisions is False
    assert cfg.gemini.api_key == "k"
    assert cfg.gemini.model == DEFAULT_GEMINI_MODEL
    assert cfg.gemini.voice == DEFAULT_GEMINI_VOICE
    assert cfg.gemini.judge_model == DEFAULT_JUDGE_MODEL


def test_reads_and_coerces_environment() -> None:
    cfg = load_config(
        env={
            "GEMINI_API_KEY": "k",
            "WS_HOST": "127.0.0.1",
            "WS_PORT": "9000",
            "LOG_LEVEL": "debug",
            "ATHENA_DEBUG_DECISIONS": "true",
            "ATHENA_LOG_DIR": " var/athena-logs ",
            "GEMINI_MODEL": "custom-model",
            "GEMINI_VOICE": "Puck",
            "GEMINI_JUDGE_MODEL": "custom-judge",
        },
        env_file=None,
    )
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 9000
    assert cfg.log_level is LogLevel.DEBUG
    assert cfg.log_dir == "var/athena-logs"
    assert cfg.debug_decisions is True
    assert cfg.gemini.model == "custom-model"
    assert cfg.gemini.voice == "Puck"
    assert cfg.gemini.judge_model == "custom-judge"


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


def test_webhook_defaults_are_local_and_enabled() -> None:
    cfg = load_config(env={"GEMINI_API_KEY": "k"}, env_file=None)
    assert cfg.webhook == WebhookConfig()
    assert cfg.webhook.enabled is True
    assert cfg.webhook.host == DEFAULT_WEBHOOK_HOST
    assert cfg.webhook.port == DEFAULT_WEBHOOK_PORT


def test_webhook_reads_and_coerces_environment() -> None:
    cfg = load_config(
        env={
            "GEMINI_API_KEY": "k",
            "ATHENA_WEBHOOK_ENABLED": "0",
            "ATHENA_WEBHOOK_HOST": "0.0.0.0",
            "ATHENA_WEBHOOK_PORT": "9000",
        },
        env_file=None,
    )
    assert cfg.webhook.enabled is False
    assert cfg.webhook.host == "0.0.0.0"
    assert cfg.webhook.port == 9000


@pytest.mark.parametrize(
    "overrides",
    [
        {"ATHENA_WEBHOOK_PORT": "0"},
        {"ATHENA_WEBHOOK_PORT": "70000"},
        {"ATHENA_WEBHOOK_PORT": "not-a-port"},
        {"ATHENA_WEBHOOK_ENABLED": "maybe"},
        {"ATHENA_WEBHOOK_HOST": "   "},
    ],
)
def test_invalid_webhook_config_raises_clear_validation_error(overrides) -> None:
    with pytest.raises(ValidationError):
        load_config(env={"GEMINI_API_KEY": "k", **overrides}, env_file=None)


def test_console_defaults_are_local_and_enabled() -> None:
    cfg = load_config(env={"GEMINI_API_KEY": "k"}, env_file=None)
    assert cfg.console == ConsoleConfig()
    assert cfg.console.enabled is True
    assert cfg.console.host == DEFAULT_CONSOLE_HOST
    assert cfg.console.port == DEFAULT_CONSOLE_PORT
    assert cfg.console.static_dir == DEFAULT_CONSOLE_STATIC_DIR
    assert cfg.console.trace_buffer == DEFAULT_CONSOLE_TRACE_BUFFER


def test_console_reads_and_coerces_environment() -> None:
    cfg = load_config(
        env={
            "GEMINI_API_KEY": "k",
            "ATHENA_CONSOLE_ENABLED": "0",
            "ATHENA_CONSOLE_HOST": "0.0.0.0",
            "ATHENA_CONSOLE_PORT": "9001",
            "ATHENA_CONSOLE_STATIC_DIR": "web/custom-dist",
            "ATHENA_CONSOLE_TRACE_BUFFER": "42",
        },
        env_file=None,
    )
    assert cfg.console.enabled is False
    assert cfg.console.host == "0.0.0.0"
    assert cfg.console.port == 9001
    assert cfg.console.static_dir == "web/custom-dist"
    assert cfg.console.trace_buffer == 42


@pytest.mark.parametrize(
    "overrides",
    [
        {"ATHENA_CONSOLE_PORT": "0"},
        {"ATHENA_CONSOLE_PORT": "70000"},
        {"ATHENA_CONSOLE_PORT": "not-a-port"},
        {"ATHENA_CONSOLE_ENABLED": "maybe"},
        {"ATHENA_CONSOLE_HOST": "   "},
        {"ATHENA_CONSOLE_STATIC_DIR": "   "},
        {"ATHENA_CONSOLE_TRACE_BUFFER": "0"},
        {"ATHENA_CONSOLE_TRACE_BUFFER": "-1"},
        {"ATHENA_CONSOLE_TRACE_BUFFER": "not-a-size"},
    ],
)
def test_invalid_console_config_raises_clear_validation_error(overrides) -> None:
    with pytest.raises(ValidationError):
        load_config(env={"GEMINI_API_KEY": "k", **overrides}, env_file=None)


def test_plugin_paths_default_to_the_repository_root() -> None:
    cfg = load_config(env={"GEMINI_API_KEY": "k"}, env_file=None)

    assert cfg.plugins == PluginsConfig()
    assert cfg.plugins.directory == "plugins"
    assert cfg.plugins.state_path == "plugins.json"


def test_plugin_paths_are_configurable_from_the_environment() -> None:
    cfg = load_config(
        env={
            "GEMINI_API_KEY": "k",
            "ATHENA_PLUGINS_DIR": " var/plugins ",
            "ATHENA_PLUGINS_STATE": "var/plugins.json",
        },
        env_file=None,
    )

    assert cfg.plugins.directory == "var/plugins"
    assert cfg.plugins.state_path == "var/plugins.json"
