from __future__ import annotations

import json
import stat
from pathlib import Path

from client.settings import (
    DEFAULT_SERVER_URL,
    Coupling,
    SettingsStore,
    StoredCoupling,
    resolve_coupling,
)


def test_a_missing_store_is_empty(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "client.json")

    assert store.load() == StoredCoupling()


def test_saving_then_loading_keeps_url_and_token(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "client.json")

    store.save(server_url="ws://athena.example:8765", access_token="ath_secret")

    assert store.load() == StoredCoupling(
        server_url="ws://athena.example:8765", access_token="ath_secret"
    )


def test_the_store_is_private(tmp_path: Path) -> None:
    path = tmp_path / "athena" / "client.json"
    SettingsStore(path).save(server_url="ws://local:8765", access_token="ath_secret")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_an_empty_token_keeps_the_stored_one(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path / "client.json")
    store.save(server_url="ws://local:8765", access_token="ath_secret")

    store.save(server_url="ws://other:9000", access_token=None)

    assert store.load() == StoredCoupling(
        server_url="ws://other:9000", access_token="ath_secret"
    )


def test_a_corrupt_store_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text("{ not json", encoding="utf-8")

    assert SettingsStore(path).load() == StoredCoupling()


def test_an_unknown_key_is_not_kept(tmp_path: Path) -> None:
    path = tmp_path / "client.json"
    path.write_text(
        json.dumps({"server_url": "ws://local:8765", "unexpected": "value"}),
        encoding="utf-8",
    )

    assert SettingsStore(path).load() == StoredCoupling(server_url="ws://local:8765")


def test_the_environment_token_wins_over_the_store() -> None:
    stored = StoredCoupling(server_url="ws://file:8765", access_token="ath_file")

    coupling = resolve_coupling(
        stored, {"ATHENA_ACCESS_TOKEN": "ath_env", "WS_SERVER_URL": "ws://env:1"}
    )

    assert isinstance(coupling, Coupling)
    assert coupling.server_url == "ws://env:1"
    assert coupling.url_source == "environment"
    assert coupling.access_token == "ath_env"
    assert coupling.token_source == "environment"
    assert coupling.token_configured is True


def test_the_store_is_used_when_the_environment_is_silent() -> None:
    stored = StoredCoupling(server_url="ws://file:8765", access_token=" ath_file ")

    coupling = resolve_coupling(stored, {})

    assert coupling.server_url == "ws://file:8765"
    assert coupling.url_source == "file"
    assert coupling.access_token == "ath_file"
    assert coupling.token_source == "file"


def test_defaults_apply_when_nothing_is_configured() -> None:
    coupling = resolve_coupling(StoredCoupling(), {})

    assert coupling.server_url == DEFAULT_SERVER_URL
    assert coupling.url_source == "default"
    assert coupling.access_token == ""
    assert coupling.token_source == "none"
    assert coupling.token_configured is False
