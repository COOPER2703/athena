"""Moteur de résolution des Réglages : précédence et source de chaque valeur.

La règle est figée par l'ADR-0024 : **environnement du processus > magasin >
``.env`` > défauts**. Le ``.env`` est présenté comme une valeur d'environnement,
donc il masque aussi le champ en écriture ; le magasin, lui, est éditable tant
que l'environnement ne le couvre pas.

Ce module est pur : la table et la résolution ne touchent ni disque ni réseau.
Le ticket #174 les exposera derrière ``GET/PUT /api/settings``.
"""

from __future__ import annotations

from server.core.settings import (
    SETTINGS,
    SettingSource,
    field_by_key,
    resolve_all,
    resolve_setting,
)


def _resolve(key: str, *, process_env=None, store=None, dotenv=None):
    field = field_by_key(key)
    assert field is not None, key
    return resolve_setting(
        field,
        process_env=process_env or {},
        store=store or {},
        dotenv=dotenv or {},
    )


def test_a_field_without_any_override_takes_its_default() -> None:
    voice = _resolve("voice")

    assert voice.value == "Leda"
    assert voice.source is SettingSource.DEFAULT
    assert voice.read_only is False


def test_a_stored_value_overrides_the_default() -> None:
    voice = _resolve("voice", store={"voice": "Puck"})

    assert voice.value == "Puck"
    assert voice.source is SettingSource.STORE
    assert voice.read_only is False


def test_the_dotenv_file_is_presented_as_an_environment_value() -> None:
    voice = _resolve("voice", dotenv={"GEMINI_VOICE": "Aoede"})

    assert voice.value == "Aoede"
    assert voice.source is SettingSource.ENV
    # Masqué par l'environnement : en lecture seule, avec sa source.
    assert voice.read_only is True


def test_the_store_wins_over_the_dotenv_file() -> None:
    voice = _resolve(
        "voice",
        store={"voice": "Puck"},
        dotenv={"GEMINI_VOICE": "Aoede"},
    )

    assert voice.value == "Puck"
    assert voice.source is SettingSource.STORE


def test_the_process_environment_wins_over_the_store() -> None:
    voice = _resolve(
        "voice",
        process_env={"GEMINI_VOICE": "Leda"},
        store={"voice": "Puck"},
        dotenv={"GEMINI_VOICE": "Aoede"},
    )

    assert voice.value == "Leda"
    assert voice.source is SettingSource.ENV
    assert voice.read_only is True


def test_an_empty_environment_value_does_not_mask_the_store() -> None:
    voice = _resolve(
        "voice", process_env={"GEMINI_VOICE": ""}, store={"voice": "Puck"}
    )

    assert voice.value == "Puck"
    assert voice.source is SettingSource.STORE


def test_a_read_only_path_is_never_editable() -> None:
    memory_db = _resolve("memory_db", store={"memory_db": "autre.db"})
    auth_db = field_by_key("auth_db")
    settings_db = field_by_key("settings_db")
    master_key = field_by_key("master_key_file")

    assert memory_db.read_only is True
    assert auth_db is not None and auth_db.editable is False
    assert settings_db is not None and settings_db.editable is False
    assert master_key is not None and master_key.editable is False


def test_every_field_exposes_its_environment_variable_and_default() -> None:
    fields = {field.key: field for field in SETTINGS}

    assert fields["voice"].env == "GEMINI_VOICE"
    assert fields["webhook_port"].env == "ATHENA_WEBHOOK_PORT"
    assert fields["console_port"].env == "ATHENA_CONSOLE_PORT"
    assert fields["log_level"].env == "LOG_LEVEL"
    # Aucun doublon : une seule source de vérité par réglage.
    assert len(fields) == len(SETTINGS)
    assert all(field.env for field in SETTINGS)
    assert all(field.default == fields[field.key].default for field in SETTINGS)


def test_resolve_all_returns_every_field_with_its_source() -> None:
    resolved = resolve_all(
        process_env={"GEMINI_VOICE": "Leda"},
        store={"webhook_port": "9000"},
        dotenv={"GEMINI_VOICE": "Aoede"},
    )

    by_key = {setting.key: setting for setting in resolved}
    assert len(by_key) == len(SETTINGS)
    assert by_key["voice"].source is SettingSource.ENV
    assert by_key["webhook_port"].source is SettingSource.STORE
    assert by_key["webhook_port"].value == "9000"
    assert by_key["log_level"].source is SettingSource.DEFAULT
