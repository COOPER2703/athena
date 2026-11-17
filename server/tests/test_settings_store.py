"""Magasin de réglages et de Secrets, sur une vraie base SQLite.

Le magasin est un artefact d'exploitation : ces tests observent le disque
(chiffrement au repos, permissions de la clé maître) et l'interface publique du
magasin, jamais une méthode privée. Le secret lui-même ne doit jamais
apparaître en clair dans la base.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from server.adapters.settings.resolver import LayeredSecretResolver
from server.adapters.settings.store import SqliteSettingsStore
from server.core.ports import MissingSecretError


def _store(
    tmp_path: Path,
    *,
    db_path: Path | None = None,
    key_path: Path | None = None,
) -> SqliteSettingsStore:
    return SqliteSettingsStore(
        db_path=str(db_path or (tmp_path / "settings.db")),
        key_path=str(key_path or (tmp_path / "master.key")),
    )


def test_a_secret_is_encrypted_at_rest_and_decrypts_to_its_value(
    tmp_path: Path,
) -> None:
    value = "cle-gemini-tres-secrete-9f3a"
    store = _store(tmp_path)

    status = store.set_secret("GEMINI_API_KEY", value)

    assert status.name == "GEMINI_API_KEY"
    assert status.configured is True
    assert status.updated_at is not None
    assert store.secret("GEMINI_API_KEY") == value

    stored = (tmp_path / "settings.db").read_bytes()
    assert value.encode() not in stored


def test_the_master_key_is_generated_once_and_lives_in_a_0600_file(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    key_path = tmp_path / "master.key"

    first = store.ensure_master_key()

    assert first
    assert key_path.is_file()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    # La clé n'est rendue qu'à sa création : jamais relue par l'API.
    assert store.ensure_master_key() is None
    assert key_path.read_text(encoding="utf-8").strip() == first


def test_statuses_list_names_states_and_dates_but_never_a_value(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)

    empty = {status.name: status for status in store.statuses()}
    assert empty["GEMINI_API_KEY"].configured is False
    assert empty["GEMINI_API_KEY"].updated_at is None

    store.set_secret("GEMINI_API_KEY", "valeur")

    statuses = {status.name: status for status in store.statuses()}
    assert statuses["GEMINI_API_KEY"].configured is True
    assert statuses["GEMINI_API_KEY"].updated_at is not None


def test_deleting_a_secret_removes_it_for_good(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.set_secret("GEMINI_API_KEY", "valeur")

    assert store.delete_secret("GEMINI_API_KEY") is True

    assert store.secret("GEMINI_API_KEY") is None
    statuses = {status.name: status for status in store.statuses()}
    assert statuses["GEMINI_API_KEY"].configured is False
    assert store.delete_secret("GEMINI_API_KEY") is False


def test_a_lost_master_key_marks_secrets_unreadable_without_resetting_them(
    tmp_path: Path,
) -> None:
    database = tmp_path / "settings.db"
    key_path = tmp_path / "master.key"
    store = _store(tmp_path, db_path=database, key_path=key_path)
    store.set_secret("GEMINI_API_KEY", "valeur-a-retrouver")

    key_path.unlink()
    restarted = _store(tmp_path, db_path=database, key_path=key_path)

    assert restarted.unreadable is True
    assert restarted.secret("GEMINI_API_KEY") is None
    statuses = {status.name: status for status in restarted.statuses()}
    assert statuses["GEMINI_API_KEY"].configured is True
    # Aucune réinitialisation silencieuse : la ligne est toujours là.
    assert database.is_file()
    assert b"secrets" in database.read_bytes()


def test_a_new_master_key_lets_secrets_be_entered_again(tmp_path: Path) -> None:
    key_path = tmp_path / "master.key"
    store = _store(tmp_path, key_path=key_path)
    store.set_secret("GEMINI_API_KEY", "ancienne-valeur")
    key_path.unlink()

    restarted = _store(tmp_path, key_path=key_path)
    assert restarted.unreadable is True

    restarted.set_secret("GEMINI_API_KEY", "nouvelle-valeur")

    assert restarted.secret("GEMINI_API_KEY") == "nouvelle-valeur"
    assert restarted.unreadable is False
    assert key_path.is_file()


def test_reading_a_fresh_store_does_not_create_its_database(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.secret("GEMINI_API_KEY") is None
    assert store.unreadable is False

    assert not (tmp_path / "settings.db").exists()
    assert not (tmp_path / "master.key").exists()


def test_settings_values_round_trip_as_text(tmp_path: Path) -> None:
    store = _store(tmp_path)

    store.set_value("voice", "Puck")
    store.set_value("journal_retention_days", "7")

    assert store.value("voice") == "Puck"
    assert store.value("unknown") is None
    assert store.values() == {
        "voice": "Puck",
        "journal_retention_days": "7",
    }

    assert store.delete_value("voice") is True
    assert store.value("voice") is None


def test_secret_precedence_is_environment_then_store_then_dotenv(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    resolver = LayeredSecretResolver(
        process_env={"GEMINI_API_KEY": "du-processus"},
        store=store,
        dotenv={"GEMINI_API_KEY": "du-fichier"},
    )

    assert resolver.optional("GEMINI_API_KEY") == "du-processus"

    resolver = LayeredSecretResolver(
        process_env={},
        store=store,
        dotenv={"GEMINI_API_KEY": "du-fichier"},
    )
    assert resolver.optional("GEMINI_API_KEY") == "du-fichier"

    store.set_secret("GEMINI_API_KEY", "du-magasin")
    assert resolver.optional("GEMINI_API_KEY") == "du-magasin"


def test_missing_secret_raises_with_its_name_but_never_a_value(tmp_path: Path) -> None:
    store = _store(tmp_path)
    resolver = LayeredSecretResolver(process_env={}, store=store, dotenv={})

    with pytest.raises(MissingSecretError) as excinfo:
        resolver.resolve("GEMINI_API_KEY")

    assert "GEMINI_API_KEY" in str(excinfo.value)
