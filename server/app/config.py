from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from server.adapters.memory.embedder import DEFAULT_EMBED_MODEL
from server.app.secrets import EnvSecretResolver, SecretResolver

DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_GEMINI_VOICE = "Zephyr"
DEFAULT_JUDGE_MODEL = "gemini-3.8-flash"
DEFAULT_MEMORY_DB = "athena_memory.db"
DEFAULT_CONSOLIDATION_MODEL = "gemini-2.0-flash"
DEFAULT_WEBHOOK_HOST = "127.0.0.1"
DEFAULT_WEBHOOK_PORT = 8766

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}


def _coerce_bool(value: object, *, variable: str) -> bool:
    """Coerce une valeur d'environnement en booléen, avec un message explicite."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"Expected a boolean for {variable}, got {value!r}")


class LogLevel(StrEnum):
    """Niveaux de log reconnus par la configuration."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ServerConfig(BaseModel):
    """Paramètres réseau du serveur WebSocket."""

    model_config = ConfigDict(frozen=True)

    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1, le=65535)


class GeminiConfig(BaseModel):
    """Paramètres de connexion à Gemini Live."""

    model_config = ConfigDict(frozen=True)

    api_key: str
    model: str = DEFAULT_GEMINI_MODEL
    voice: str = DEFAULT_GEMINI_VOICE
    judge_model: str = Field(default=DEFAULT_JUDGE_MODEL, min_length=1)


class MemoryConfig(BaseModel):
    """Paramètres de la mémoire (Journal, mémoire long terme, embeddings).

    #26 définit le modèle figé complet pour que les tickets suivants
    (#27 Rappel, #29 Mémorisation explicite, #30 Consolidation) n'aient plus à
    le modifier.
    """

    model_config = ConfigDict(frozen=True)

    user_id: str = Field(default="owner", min_length=1)
    db_path: str = Field(default=DEFAULT_MEMORY_DB, min_length=1)
    short_term_window: int = Field(default=20, ge=1)
    recent_events_window: int = Field(default=20, ge=1)
    recall_k: int = Field(default=5, ge=1)
    recall_window_turns: int = Field(default=3, ge=1)
    recall_max_distance: float = Field(default=0.6, ge=0.0, le=2.0)
    dedup_threshold: float = Field(default=0.15, ge=0.0, le=2.0)
    journal_retention_days: int = Field(default=3, ge=1)
    embed_model: str = Field(default=DEFAULT_EMBED_MODEL, min_length=1)
    consolidation_model: str = Field(
        default=DEFAULT_CONSOLIDATION_MODEL, min_length=1
    )
    consolidation_hours: tuple[int, ...] = (0, 12)

    @field_validator(
        "user_id", "db_path", "embed_model", "consolidation_model", mode="before"
    )
    @classmethod
    def _strip_non_empty(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("consolidation_hours", mode="before")
    @classmethod
    def _parse_hours(cls, value: object) -> object:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
            return [int(part) for part in parts]
        return value

    @field_validator("consolidation_hours")
    @classmethod
    def _check_hours(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("ATHENA_CONSOLIDATION_HOURS must list at least one hour")
        for hour in value:
            if not 0 <= hour <= 23:
                raise ValueError(
                    f"ATHENA_CONSOLIDATION_HOURS hours must be in [0, 23], got {hour}"
                )
        return value


class WebhookConfig(BaseModel):
    """Paramètres de l'adaptateur Webhook HTTP (ADR-0011).

    Lié à ``127.0.0.1`` par défaut : la surface n'est ouverte qu'à la machine
    locale, sans authentification. L'exposition au LAN (jeton partagé) est hors
    périmètre de ce lot.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    host: str = Field(default=DEFAULT_WEBHOOK_HOST, min_length=1)
    port: int = Field(default=DEFAULT_WEBHOOK_PORT, ge=1, le=65535)

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled(cls, value: object) -> object:
        return _coerce_bool(value, variable="ATHENA_WEBHOOK_ENABLED")

    @field_validator("host", mode="before")
    @classmethod
    def _strip_host(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class AppConfig(BaseModel):
    """Configuration validée au démarrage, injectée par constructeur."""

    model_config = ConfigDict(frozen=True)

    server: ServerConfig = Field(default_factory=ServerConfig)
    gemini: GeminiConfig
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    log_level: LogLevel = LogLevel.INFO
    debug_decisions: bool = False

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_level(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("debug_decisions", mode="before")
    @classmethod
    def _debug_decisions(cls, value: object) -> object:
        return _coerce_bool(value, variable="ATHENA_DEBUG_DECISIONS")


def parse_dotenv(path: str | Path) -> dict[str, str]:
    """Lit un fichier ``.env`` simple en paires clé/valeur."""
    values: dict[str, str] = {}
    file_path = Path(path)
    if not file_path.is_file():
        return values
    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
    secrets: SecretResolver | None = None,
) -> AppConfig:
    """Construit la configuration depuis l'environnement et un fichier ``.env``.

    L'environnement prime sur le fichier. Le secret Gemini est résolu via le
    ``SecretResolver`` injecté (ou un résolveur d'environnement par défaut) :
    une variable manquante lève ``MissingSecretError``.
    """
    file_values = parse_dotenv(env_file) if env_file is not None else {}
    process_env = os.environ if env is None else env
    merged: dict[str, str] = {**file_values, **process_env}

    resolver: SecretResolver = (
        secrets if secrets is not None else EnvSecretResolver(merged)
    )
    api_key = resolver.resolve("GEMINI_API_KEY")

    return AppConfig(
        server=ServerConfig(
            host=merged.get("WS_HOST", "0.0.0.0"),
            port=merged.get("WS_PORT", "8765"),
        ),
        gemini=GeminiConfig(
            api_key=api_key,
            model=merged.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
            voice=merged.get("GEMINI_VOICE", DEFAULT_GEMINI_VOICE),
            judge_model=merged.get("GEMINI_JUDGE_MODEL", DEFAULT_JUDGE_MODEL),
        ),
        memory=MemoryConfig(
            user_id=merged.get("ATHENA_USER_ID", "owner"),
            db_path=merged.get("ATHENA_MEMORY_DB", DEFAULT_MEMORY_DB),
            short_term_window=merged.get("ATHENA_SHORT_TERM_WINDOW", "20"),
            recent_events_window=merged.get("ATHENA_RECENT_EVENTS_WINDOW", "20"),
            recall_k=merged.get("ATHENA_RECALL_K", "5"),
            recall_window_turns=merged.get("ATHENA_RECALL_WINDOW_TURNS", "3"),
            recall_max_distance=merged.get("ATHENA_RECALL_MAX_DISTANCE", "0.6"),
            dedup_threshold=merged.get("ATHENA_DEDUP_THRESHOLD", "0.15"),
            journal_retention_days=merged.get("ATHENA_JOURNAL_RETENTION_DAYS", "3"),
            embed_model=merged.get("ATHENA_EMBED_MODEL", DEFAULT_EMBED_MODEL),
            consolidation_model=merged.get(
                "ATHENA_CONSOLIDATION_MODEL", DEFAULT_CONSOLIDATION_MODEL
            ),
            consolidation_hours=merged.get("ATHENA_CONSOLIDATION_HOURS", "0,12"),
        ),
        webhook=WebhookConfig(
            enabled=merged.get("ATHENA_WEBHOOK_ENABLED", "1"),
            host=merged.get("ATHENA_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST),
            port=merged.get("ATHENA_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT)),
        ),
        log_level=merged.get("LOG_LEVEL", "INFO"),
        debug_decisions=merged.get("ATHENA_DEBUG_DECISIONS", "0"),
    )
