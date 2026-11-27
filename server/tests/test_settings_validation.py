"""Validation pure des Réglages : bornes, formes, attente et confirmation.

Ces tests couvrent la logique sans entrée/sortie que le ticket #174 partage
entre la route et le démarrage : validation d'un objet complet, calcul des
champs en attente et règle de confirmation d'URL. Les scénarios de bout en bout
(HTTP, magasin réel, redémarrage) vivent dans ``test_app_settings.py``.
"""

from __future__ import annotations

from typing import Mapping

from server.app.config import SETTING_TARGETS
from server.core.settings import (
    SETTINGS,
    SURFACES,
    SettingField,
    SettingSource,
    SettingsView,
    describe_settings,
    normalize_value,
    surface_url,
    system_timezone_name,
    validate_settings_update,
)


def _states(
    *,
    loaded: Mapping[str, str] | None = None,
    store: Mapping[str, str] | None = None,
    applied: Mapping[str, str] | None = None,
    process_env: Mapping[str, str] | None = None,
):
    values = {field.key: field.default for field in SETTINGS}
    values.update(loaded or {})
    view = SettingsView(
        loaded=values,
        applied_store=applied or {},
        process_env=process_env or {},
    )
    return describe_settings(view, store or {})


def _complete(**overrides: str) -> dict[str, object]:
    """L'objet complet qu'un PUT doit porter, défauts inclus."""
    values = {
        field.key: field.default
        for field in SETTINGS
        if field.editable
    }
    values.update(overrides)
    return {"values": values}


def test_every_setting_has_a_configuration_target() -> None:
    """Un réglage déclaré sans champ de configuration serait invisible au GET."""
    assert {field.key for field in SETTINGS} == set(SETTING_TARGETS)


def test_a_field_without_override_is_reported_as_default() -> None:
    states = {state.key: state for state in _states()}

    assert states["voice"].source is SettingSource.DEFAULT
    assert states["voice"].value == "Leda"
    assert states["voice"].read_only is False
    assert states["voice"].pending is False


def test_a_stored_value_explains_the_pending_change() -> None:
    """La valeur stockée n'est pas active tant que le processus n'a pas repris."""
    states = {
        state.key: state
        for state in _states(store={"voice": "Puck"})
    }

    voice = states["voice"]
    assert voice.value == "Leda"
    assert voice.source is SettingSource.DEFAULT
    assert voice.pending is True
    assert voice.pending_value == "Puck"


def test_a_store_value_that_was_loaded_is_the_source() -> None:
    states = {
        state.key: state
        for state in _states(
            loaded={"voice": "Puck"}, store={"voice": "Puck"},
            applied={"voice": "Puck"},
        )
    }

    assert states["voice"].source is SettingSource.STORE
    assert states["voice"].pending is False


def test_a_masked_field_is_read_only_and_never_pending() -> None:
    states = {
        state.key: state
        for state in _states(
            loaded={"voice": "Puck"},
            store={"voice": "Aoede"},
            process_env={"GEMINI_VOICE": "Puck"},
        )
    }

    voice = states["voice"]
    assert voice.source is SettingSource.ENV
    assert voice.read_only is True
    assert voice.pending is False


def test_a_complete_object_is_accepted_and_a_partial_one_refused() -> None:
    update, errors = validate_settings_update(_complete(), states=_states())
    assert errors == {}
    assert update is not None and update.writes == {} and update.removals == ()

    partial = _complete()
    values = dict(partial["values"])  # type: ignore[arg-type]
    del values["voice"]
    partial["values"] = values

    update, errors = validate_settings_update(partial, states=_states())
    assert update is None
    assert "voice" in errors


def test_types_bounds_and_choices_are_reported_per_field() -> None:
    _, errors = validate_settings_update(
        _complete(
            webhook_port="70000",
            recall_k="zéro",
            recall_max_distance="3.5",
            log_level="SHOUTY",
            consolidation_hours="24",
            webhook_enabled="peut-être",
        ),
        states=_states(),
    )

    assert set(errors) == {
        "webhook_port",
        "recall_k",
        "recall_max_distance",
        "log_level",
        "consolidation_hours",
        "webhook_enabled",
    }


def test_the_timezone_field_is_declared_with_its_own_kind() -> None:
    states = {state.key: state for state in _states()}

    assert states["timezone"].kind == "timezone"
    assert states["schedule_db"].kind == "string"
    assert states["schedule_db"].read_only is True


def test_an_invalid_timezone_is_refused_per_field_without_any_write() -> None:
    update, errors = validate_settings_update(
        _complete(timezone="Mars/Olympus", voice="Puck"),
        states=_states(),
    )

    assert update is None
    assert set(errors) == {"timezone"}
    assert "fuseau" in errors["timezone"].lower()


def test_a_valid_timezone_is_written_in_its_iana_form() -> None:
    # Une valeur égale au défaut système devient un retour au défaut, pas une
    # écriture : le test choisit donc un fuseau distinct de celui de la machine.
    target = (
        "Europe/Paris"
        if system_timezone_name() != "Europe/Paris"
        else "Asia/Tokyo"
    )
    update, errors = validate_settings_update(
        _complete(timezone=f" {target} "),
        states=_states(loaded={"timezone": "UTC"}),
    )

    assert errors == {}
    assert update is not None
    assert update.writes == {"timezone": target}


def test_values_are_written_in_their_canonical_form() -> None:
    update, errors = validate_settings_update(
        _complete(
            consolidation_hours=" 06, 18 ",
            log_level="debug",
            webhook_enabled="0",
            recall_k="007",
        ),
        states=_states(),
    )

    assert errors == {}
    assert update is not None
    assert update.writes == {
        "consolidation_hours": "6,18",
        "log_level": "DEBUG",
        "webhook_enabled": "0",
        "recall_k": "7",
    }


def test_returning_a_value_unchanged_writes_nothing() -> None:
    """Un aller-retour GET → PUT ne fige pas les valeurs déjà en vigueur."""
    update, errors = validate_settings_update(
        {"values": {field.key: field.default for field in SETTINGS}},
        states=_states(),
    )

    assert errors == {}
    assert update is not None
    assert update.writes == {}
    assert update.removals == ()


def test_a_default_value_removes_the_stored_override() -> None:
    update, errors = validate_settings_update(
        _complete(voice="Leda"),
        states=_states(store={"voice": "Puck"}),
    )

    assert errors == {}
    assert update is not None
    assert update.removals == ("voice",)
    assert update.writes == {}


def test_a_host_change_requires_the_exact_computed_url() -> None:
    states = _states(
        loaded={"console_host": "10.0.0.5", "console_port": "8767"}
    )

    update, errors = validate_settings_update(
        _complete(console_host="10.0.0.6"), states=states
    )
    assert update is None
    assert "confirm_url" in errors
    assert "http://10.0.0.6:8767" in errors["confirm_url"]

    update, errors = validate_settings_update(
        {**_complete(console_host="10.0.0.6"), "confirm_url": "http://10.0.0.6:8767"},
        states=states,
    )
    assert errors == {}
    assert update is not None
    assert update.writes["console_host"] == "10.0.0.6"

    update, errors = validate_settings_update(
        {**_complete(console_host="10.0.0.6"), "confirm_url": "http://10.0.0.6:9/"},
        states=states,
    )
    assert update is None
    assert "confirm_url" in errors


def test_several_changed_surfaces_are_all_confirmed() -> None:
    states = _states(
        loaded={
            "console_host": "127.0.0.1",
            "console_port": "8767",
            "server_host": "127.0.0.1",
            "server_port": "8765",
        }
    )
    values = _complete(
        console_port="9000", server_port="9001", server_host="127.0.0.1"
    )

    _, errors = validate_settings_update(
        {**values, "confirm_url": "http://127.0.0.1:9000"}, states=states
    )
    assert "confirm_url" in errors

    update, errors = validate_settings_update(
        {
            **values,
            "confirm_url": ("http://127.0.0.1:9000", "ws://127.0.0.1:9001"),
        },
        states=states,
    )
    assert errors == {}
    assert update is not None


def test_an_ipv6_host_keeps_brackets_in_the_computed_url() -> None:
    surface = next(item for item in SURFACES if item.name == "console")

    assert surface_url(surface, "::1", "8767") == "http://[::1]:8767"


def test_an_invalid_host_is_refused_without_echoing_it() -> None:
    _, message = normalize_value(
        SettingField("console_host", "ATHENA_CONSOLE_HOST", "127.0.0.1"),
        "http://exemple/console",
    )
    assert message is not None
    assert "exemple" not in message
