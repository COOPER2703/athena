from __future__ import annotations

import pytest

from server.app.secrets import EnvSecretResolver, MissingSecretError


def test_resolver_returns_value_from_env_mapping() -> None:
    resolver = EnvSecretResolver({"GEMINI_API_KEY": "abc"})
    assert resolver.resolve("GEMINI_API_KEY") == "abc"


def test_resolver_raises_clear_error_when_missing() -> None:
    resolver = EnvSecretResolver({})
    with pytest.raises(MissingSecretError) as excinfo:
        resolver.resolve("GEMINI_API_KEY")
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_resolver_treats_empty_value_as_missing() -> None:
    resolver = EnvSecretResolver({"GEMINI_API_KEY": ""})
    with pytest.raises(MissingSecretError):
        resolver.resolve("GEMINI_API_KEY")


def test_env_resolver_defaults_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_TEST_SECRET", "s3cr3t")
    assert EnvSecretResolver().resolve("ATHENA_TEST_SECRET") == "s3cr3t"
