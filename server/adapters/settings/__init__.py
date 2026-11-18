"""Adaptateur du magasin de réglages et de Secrets (ADR-0025)."""

from __future__ import annotations

from server.adapters.settings.crypto import (
    FORMAT_VERSION,
    MASTER_KEY_BYTES,
    MasterKeyFile,
    seal,
    unseal,
)
from server.adapters.settings.resolver import LayeredSecretResolver
from server.adapters.settings.store import SqliteSettingsStore

__all__ = [
    "FORMAT_VERSION",
    "MASTER_KEY_BYTES",
    "LayeredSecretResolver",
    "MasterKeyFile",
    "SqliteSettingsStore",
    "seal",
    "unseal",
]
