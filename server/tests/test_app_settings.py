"""Réglages exposés par la Console : lecture, écriture, application (ticket #174).

Tout passe par le vrai serveur du harnais : vraies requêtes HTTP sur la Console
réelle, vrais répertoires. Les scénarios observent ce qu'un navigateur et un
opérateur voient — la valeur effective, sa source, ce qui attend un redémarrage,
et jamais une valeur de Secret.

La règle de précédence est celle de l'ADR-0025 : **environnement du processus >
magasin > ``.env`` > défauts**. « Changements en attente » est l'écart entre les
valeurs stockées et celles que le processus a chargées au démarrage.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from harness import TEST_PASSWORD
from server.adapters.console.contract import (
    LOGIN_PATH,
    SECRETS_PATH,
    SETTINGS_PATH,
)
from server_harness import running_server

GEMINI_KEY = "cle-gemini-reglages-6f21"


# ---------------------------------------------------------------------------
# Aides HTTP : le contrat, rien d'autre
# ---------------------------------------------------------------------------


async def _get_settings(http, server) -> dict[str, Any]:
    async with http.get(f"{server.url}{SETTINGS_PATH}") as response:
        assert response.status == 200, await response.text()
        return await response.json()


async def _put_settings(
    http,
    server,
    values: Mapping[str, object],
    *,
    confirm_url: object | None = None,
) -> tuple[int, dict[str, Any]]:
    payload: dict[str, object] = {"values": dict(values)}
    if confirm_url is not None:
        payload["confirm_url"] = confirm_url
    async with http.put(f"{server.url}{SETTINGS_PATH}", json=payload) as response:
        return response.status, await response.json()


def _editable_values(settings: Mapping[str, Any]) -> dict[str, str]:
    """L'objet complet qu'un PUT doit porter : tout champ éditable et non masqué."""
    return {
        key: field["value"]
        for key, field in settings["fields"].items()
        if not field["read_only"]
    }


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------


def test_get_settings_reports_the_effective_value_default_and_source(
    tmp_path: Path,
) -> None:
    """Chaque champ porte sa valeur, son défaut, sa source et sa lisibilité."""

    async def scenario() -> None:
        async with running_server(
            tmp_path,
            gemini_key=GEMINI_KEY,
            # L'environnement du processus masque la voix : source ``env``.
            env={"GEMINI_VOICE": "Puck"},
        ) as server:
            http = await server.install()
            settings = await _get_settings(http, server)

            assert settings["v"] == 1
            voice = settings["fields"]["voice"]
            assert voice == {
                "env": "GEMINI_VOICE",
                "kind": "string",
                "minimum": None,
                "maximum": None,
                "choices": None,
                "value": "Puck",
                "default": "Leda",
                "source": "env",
                "editable": True,
                "read_only": True,
                "pending": False,
                "pending_value": None,
            }
            log_level = settings["fields"]["log_level"]
            assert log_level["value"] == "INFO"
            assert log_level["source"] == "default"
            assert log_level["read_only"] is False
            assert set(log_level["choices"]) == {
                "DEBUG",
                "INFO",
                "WARNING",
                "ERROR",
                "CRITICAL",
            }
            # Le format du champ guide le formulaire du front (#175).
            port = settings["fields"]["webhook_port"]
            assert port["kind"] == "int"
            assert port["minimum"] == 1
            assert port["maximum"] == 65535

            # Les chemins sont structurellement en lecture seule et montrent la
            # valeur réellement chargée par la racine.
            memory_db = settings["fields"]["memory_db"]
            assert memory_db["editable"] is False
            assert memory_db["read_only"] is True
            assert memory_db["value"] == str(server.paths.memory_db)
            for key in (
                "auth_db",
                "settings_db",
                "schedule_db",
                "master_key_file",
                "plugins_dir",
                "plugins_state",
                "log_dir",
                "embed_model",
            ):
                assert settings["fields"][key]["read_only"] is True, key

            # Le front reçoit le couplage hôte/port de chaque surface et l'URL
            # active, pour proposer la nouvelle adresse sans redire la règle.
            surfaces = {item["name"]: item for item in settings["surfaces"]}
            assert surfaces["console"] == {
                "name": "console",
                "host": "console_host",
                "port": "console_port",
                "scheme": "http",
                "url": server.url,
            }
            assert surfaces["transport"]["scheme"] == "ws"
            assert surfaces["transport"]["url"].startswith("ws://")
            assert surfaces["webhook"]["scheme"] == "http"

            # Aucun changement n'attend encore, et le bandeau nomme la commande.
            assert settings["pending"] == []
            assert settings["restart_required"] is False
            assert settings["restart"] == {
                "required": False,
                "automatic": False,
                "command": "docker restart athena",
                "requested": False,
            }
            # Lire les Réglages ne crée pas le magasin : une instance qui n'a
            # rien enregistré ne fait pas écrire de base au démarrage.
            assert not server.paths.settings_db.exists()

    asyncio.run(scenario())


def test_settings_require_the_console_ticket(tmp_path: Path) -> None:
    """Une surface installée ne livre aucun réglage sans Ticket Console."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            async with server.anonymous() as http:
                async with http.get(f"{server.url}{SETTINGS_PATH}") as response:
                    assert response.status == 401
                    assert (await response.json())["error"] == "unauthorized"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Écriture et changements en attente
# ---------------------------------------------------------------------------


def test_put_settings_writes_the_whole_object_and_reports_pending(
    tmp_path: Path,
) -> None:
    """La valeur enregistrée n'est pas active : elle attend le redémarrage."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            before = await _get_settings(http, server)
            values = _editable_values(before)
            values["voice"] = "Puck"

            status, after = await _put_settings(http, server, values)

            assert status == 200, after
            assert after["fields"]["voice"]["value"] == "Leda"
            assert after["fields"]["voice"]["pending"] is True
            assert after["fields"]["voice"]["pending_value"] == "Puck"
            assert after["pending"] == [
                {"key": "voice", "active": "Leda", "stored": "Puck"}
            ]
            assert after["restart_required"] is True
            assert after["restart"]["required"] is True
            assert after["restart"]["automatic"] is False
            assert after["restart"]["requested"] is False

            # Le GET suivant relit le magasin : rien n'est perdu.
            again = await _get_settings(http, server)
            assert again["pending"] == after["pending"]
            assert again["fields"]["voice"]["pending_value"] == "Puck"

    asyncio.run(scenario())


def test_a_value_equal_to_its_default_removes_the_stored_value(
    tmp_path: Path,
) -> None:
    """Revenir au défaut efface la personnalisation au lieu de la figer."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            before = await _get_settings(http, server)
            values = _editable_values(before)

            values["voice"] = "Puck"
            status, stored = await _put_settings(http, server, values)
            assert status == 200, stored
            assert stored["restart_required"] is True

            values["voice"] = before["fields"]["voice"]["default"]
            status, cleared = await _put_settings(http, server, values)
            assert status == 200, cleared
            assert cleared["fields"]["voice"]["source"] == "default"
            assert cleared["fields"]["voice"]["pending"] is False
            assert cleared["pending"] == []
            assert cleared["restart_required"] is False

    asyncio.run(scenario())


def test_put_settings_rejects_a_partial_object_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """« Objet complet » : un champ manquant refuse tout l'objet, sans écriture."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            values = _editable_values(await _get_settings(http, server))
            del values["recall_k"]

            status, body = await _put_settings(http, server, values)

            assert status == 400
            assert body["error"] == "invalid_settings"
            assert "recall_k" in body["fields"]
            assert (await _get_settings(http, server))["pending"] == []

    asyncio.run(scenario())


def test_put_settings_reports_every_invalid_field_at_once(tmp_path: Path) -> None:
    """Tous les défauts sont rendus ensemble ; rien n'est écrit entre-temps."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            values = _editable_values(await _get_settings(http, server))
            values.update(
                {
                    "webhook_port": "0",
                    "recall_k": "-1",
                    "log_level": "SHOUTY",
                    "consolidation_hours": "25",
                    "user_id": "   ",
                    "voice": "Puck",
                }
            )

            status, body = await _put_settings(http, server, values)

            assert status == 400
            assert body["error"] == "invalid_settings"
            for key in (
                "webhook_port",
                "recall_k",
                "log_level",
                "consolidation_hours",
                "user_id",
            ):
                assert key in body["fields"], key
            # Le champ valide de la même requête n'a pas été appliqué.
            settings = await _get_settings(http, server)
            assert settings["pending"] == []

    asyncio.run(scenario())


def test_an_unknown_setting_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            values = _editable_values(await _get_settings(http, server))
            values["voix"] = "Puck"

            status, body = await _put_settings(http, server, values)

            assert status == 400
            assert "voix" in body["fields"]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Lecture seule : environnement et chemins
# ---------------------------------------------------------------------------


def test_the_environment_masks_a_field_and_the_put_refuses_it(
    tmp_path: Path,
) -> None:
    """Un champ masqué est refusé explicitement, pas ignoré en silence."""

    async def scenario() -> None:
        async with running_server(
            tmp_path, env={"GEMINI_VOICE": "Puck"}
        ) as server:
            http = await server.install()
            before = await _get_settings(http, server)
            values = _editable_values(before)
            # Le champ masqué n'appartient pas à l'objet complet...
            assert "voice" not in values

            values["voice"] = "Aoede"
            status, body = await _put_settings(http, server, values)

            assert status == 400
            assert "voice" in body["fields"]
            assert "environnement" in body["fields"]["voice"]
            assert (await _get_settings(http, server))["pending"] == []

            # ... mais renvoyer la valeur déjà en vigueur n'écrit rien et passe.
            values["voice"] = "Puck"
            status, accepted = await _put_settings(http, server, values)
            assert status == 200, accepted
            assert accepted["pending"] == []

    asyncio.run(scenario())


def test_a_path_is_refused_by_name_even_with_a_valid_looking_value(
    tmp_path: Path,
) -> None:
    """Un chemin valide mais différent est tout aussi destructeur : refus."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            before = await _get_settings(http, server)
            values = _editable_values(before)
            values["memory_db"] = str(tmp_path / "autre-memoire.db")

            status, body = await _put_settings(http, server, values)

            assert status == 400
            assert body["error"] == "invalid_settings"
            assert "memory_db" in body["fields"]
            assert "lecture seule" in body["fields"]["memory_db"]

            after = await _get_settings(http, server)
            assert after["fields"]["memory_db"]["value"] == str(
                before["fields"]["memory_db"]["value"]
            )
            assert after["pending"] == []

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Hôtes et ports : confirmation explicite de la nouvelle URL
# ---------------------------------------------------------------------------


def test_a_host_or_port_change_requires_the_computed_url(tmp_path: Path) -> None:
    """Sans l'URL exacte, un changement d'adresse est refusé champ par champ."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            before = await _get_settings(http, server)
            values = _editable_values(before)
            host = before["fields"]["console_host"]["value"]
            values["console_port"] = "8797"

            status, body = await _put_settings(http, server, values)
            assert status == 400
            assert "confirm_url" in body["fields"]
            assert f"http://{host}:8797" in body["fields"]["confirm_url"]

            # Une URL qui ne correspond pas est refusée de la même façon.
            status, body = await _put_settings(
                http, server, values, confirm_url=f"http://{host}:8798"
            )
            assert status == 400
            assert "confirm_url" in body["fields"]

            status, accepted = await _put_settings(
                http, server, values, confirm_url=f"http://{host}:8797"
            )
            assert status == 200, accepted
            assert {
                (change["key"], change["stored"])
                for change in accepted["pending"]
            } == {("console_port", "8797")}

            # Le transport est une surface WebSocket : son URL est en ``ws``.
            values["server_port"] = str(int(before["fields"]["server_port"]["value"]) + 1)
            status, body = await _put_settings(
                http, server, values, confirm_url=f"http://{host}:8797"
            )
            assert status == 400
            assert "confirm_url" in body["fields"]
            transport_url = f"ws://{host}:{values['server_port']}"
            status, accepted = await _put_settings(
                http,
                server,
                values,
                confirm_url=(f"http://{host}:8797", transport_url),
            )
            assert status == 200, accepted
            assert {change["key"] for change in accepted["pending"]} == {
                "console_port",
                "server_port",
            }

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Application : redémarrage automatique ou bandeau
# ---------------------------------------------------------------------------


def test_apply_restarts_the_process_when_the_flag_allows(tmp_path: Path) -> None:
    """Le drapeau autorise une sortie propre ; la réponse part avant l'arrêt."""

    async def scenario() -> None:
        restarts: list[None] = []
        async with running_server(
            tmp_path,
            restart_on_apply=True,
            restart=lambda: restarts.append(None),
        ) as server:
            http = await server.install()
            values = _editable_values(await _get_settings(http, server))
            values["voice"] = "Puck"

            status, body = await _put_settings(http, server, values)

            assert status == 200, body
            assert body["restart"]["automatic"] is True
            assert body["restart"]["requested"] is True
            await server.wait_until(lambda: bool(restarts), timeout=2.0)
            # Le serveur de test n'est pas tué : le redémarrage est injecté.
            assert restarts == [None]

    asyncio.run(scenario())


def test_the_real_restart_seam_stops_the_server_cleanly(tmp_path: Path) -> None:
    """Sans callable injectée, le drapeau arrête vraiment le serveur."""

    async def scenario() -> None:
        async with running_server(tmp_path, restart_on_apply=True) as server:
            http = await server.install()
            values = _editable_values(await _get_settings(http, server))
            values["voice"] = "Puck"

            status, body = await _put_settings(http, server, values)

            assert status == 200, body
            assert body["restart"]["requested"] is True
            await server.wait_stopped()

    asyncio.run(scenario())


def test_no_restart_is_requested_when_the_flag_is_off(tmp_path: Path) -> None:
    """Sans drapeau, le bandeau et la commande suffisent."""

    async def scenario() -> None:
        restarts: list[None] = []
        async with running_server(
            tmp_path, restart=lambda: restarts.append(None)
        ) as server:
            http = await server.install()
            values = _editable_values(await _get_settings(http, server))
            values["voice"] = "Puck"

            status, body = await _put_settings(http, server, values)

            assert status == 200, body
            assert body["restart"]["automatic"] is False
            assert body["restart"]["requested"] is False
            assert body["restart"]["command"] == "docker restart athena"
            await asyncio.sleep(0.2)
            assert restarts == []

    asyncio.run(scenario())


def test_the_restart_command_is_configurable(tmp_path: Path) -> None:
    """La commande exacte suit le déploiement : systemd n'est pas Docker."""

    async def scenario() -> None:
        async with running_server(
            tmp_path, restart_command="systemctl restart athena"
        ) as server:
            http = await server.install()
            settings = await _get_settings(http, server)
            assert settings["restart"]["command"] == "systemctl restart athena"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Secrets : noms et états, jamais de valeur
# ---------------------------------------------------------------------------


def test_get_settings_lists_secrets_without_ever_showing_a_value(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as server:
            http = await server.install()
            async with http.put(
                f"{server.url}{SECRETS_PATH}/GEMINI_API_KEY",
                json={"value": GEMINI_KEY},
            ) as response:
                assert response.status == 200, await response.text()

            async with http.get(f"{server.url}{SETTINGS_PATH}") as response:
                assert response.status == 200
                raw = await response.text()
            body = await response.json()

            # La valeur n'est nulle part dans la réponse, même codée.
            assert GEMINI_KEY not in raw
            entries = {entry["name"]: entry for entry in body["secrets"]}
            assert entries["GEMINI_API_KEY"]["configured"] is True
            assert isinstance(entries["GEMINI_API_KEY"]["updated_at"], float)
            assert "value" not in entries["GEMINI_API_KEY"]
            assert body["secrets_unreadable"] is False

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Application au redémarrage : le magasin nourrit la configuration
# ---------------------------------------------------------------------------


def test_a_stored_value_takes_effect_after_a_restart(tmp_path: Path) -> None:
    """Le magasin est relu au démarrage : la valeur stockée devient effective."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            http = await server.install()
            values = _editable_values(await _get_settings(http, server))
            values["voice"] = "Puck"
            status, body = await _put_settings(http, server, values)
            assert status == 200, body
            assert body["restart_required"] is True

        # Nouveau processus sur le même répertoire : la config est rechargée.
        async with running_server(tmp_path) as server:
            async with server.console() as http:
                async with http.post(
                    f"{server.url}{LOGIN_PATH}",
                    json={"username": "admin", "password": TEST_PASSWORD},
                ) as response:
                    assert response.status == 200, await response.text()

                settings = await _get_settings(http, server)

            voice = settings["fields"]["voice"]
            assert voice["value"] == "Puck"
            assert voice["source"] == "store"
            assert voice["read_only"] is False
            assert settings["pending"] == []
            assert settings["restart_required"] is False

    asyncio.run(scenario())


def test_a_stored_read_only_path_is_ignored_at_startup(tmp_path: Path) -> None:
    """Le magasin ne peut pas déplacer une base, même édité à la main."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()

        # Écriture directe dans le magasin, comme le ferait une main distraite :
        # l'API la refuse, le démarrage doit l'ignorer aussi.
        _store_value(server.paths.settings_db, "memory_db", "/tmp/ailleurs.db")

        async with running_server(tmp_path) as server:
            async with server.console() as http:
                async with http.post(
                    f"{server.url}{LOGIN_PATH}",
                    json={"username": "admin", "password": TEST_PASSWORD},
                ) as response:
                    assert response.status == 200, await response.text()
                settings = await _get_settings(http, server)

            memory_db = settings["fields"]["memory_db"]
            assert memory_db["value"] == str(server.paths.memory_db)
            assert memory_db["pending"] is False
            assert settings["pending"] == []

    asyncio.run(scenario())


def _store_value(db_path: Path, key: str, value: str) -> None:
    """Écrit une valeur brute dans le magasin, hors de l'API."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            " key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value), 0.0),
        )
        connection.commit()
    finally:
        connection.close()
