"""Instantané étendu du Diagnostic : Installation et santé (ticket #177).

Tout passe par le vrai serveur du harnais : vraies requêtes HTTP, vrai flux
d'observation, vrais fichiers. Les scénarios n'inspectent que le contrat public
``GET /api/status`` et la trame ``hello`` du WebSocket, jamais le câblage.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from server import __version__
from server.adapters.console.contract import (
    SECRETS_PATH,
    STATUS_PATH,
)
from server_harness import running_server

GEMINI_KEY = "cle-gemini-diagnostic-3f91"


def _disk_bytes(path: Path) -> int:
    """Taille d'une base et de ses annexes SQLite, comme le bandeau la compte."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.is_file():
            total += candidate.stat().st_size
    return total


def test_status_carries_installation_health_and_no_secret(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()

            snapshot = await server.status()

            assert snapshot["v"] == 1
            assert snapshot["installation"] == {
                "account_exists": True,
                "gemini_configured": True,
                "complete": True,
                "secrets_unreadable": False,
                "surfaces": {
                    "gemini": True,
                    "transport": True,
                    "webhook": False,
                },
            }
            assert snapshot["restart_required"] is False

            health = snapshot["health"]
            assert health["version"] == __version__
            assert health["uptime_seconds"] >= 0
            # Le planificateur rattrape le créneau manqué au démarrage : une
            # Consolidation est donc déjà consignée, sans que rien n'ait été dit.
            consolidation = health["last_consolidation"]
            assert consolidation is not None
            assert consolidation["at"] > 0
            assert set(consolidation) == {
                "at",
                "facts_extracted",
                "facts_stored",
                "duplicates",
                "purged",
                "transcript_chars",
            }
            assert health["last_error"] is None
            assert set(health["databases"]) == {
                "memory",
                "auth",
                "settings",
                "schedule",
            }
            assert all(size >= 0 for size in health["databases"].values())
            assert health["trace_buffer"]["capacity"] == 500
            assert health["trace_buffer"]["size"] >= 0

            # Ni la clé de test ni un champ de configuration sensible ne
            # s'invitent dans l'instantané.
            serialized = json.dumps(snapshot, ensure_ascii=False)
            assert "test-key" not in serialized
            assert "api_key" not in serialized

            # Le bandeau arrive par le même instantané dans la trame `hello`.
            async with server.trace_stream() as stream:
                hello = stream.hello["data"]["snapshot"]
            assert hello["health"]["trace_buffer"]["capacity"] == 500
            assert hello["installation"]["complete"] is True

    asyncio.run(scenario())


def test_uptime_follows_the_server_monotonic_clock(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()

            first = (await server.status())["health"]["uptime_seconds"]
            assert first == 0.0

            server.clock.advance(90.0)

            second = (await server.status())["health"]["uptime_seconds"]
            assert second == 90.0

    asyncio.run(scenario())


def test_database_sizes_match_the_files_on_disk(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()

            databases = (await server.status())["health"]["databases"]

            # Une base jamais ouverte vaut 0, jamais une erreur : la mémoire et
            # les réglages peuvent ne pas exister encore, l'auth si.
            assert databases["memory"] == _disk_bytes(server.paths.memory_db)
            assert databases["auth"] == _disk_bytes(server.paths.auth_db)
            assert databases["settings"] == _disk_bytes(server.paths.settings_db)
            assert databases["auth"] > 0

    asyncio.run(scenario())


def test_the_last_error_is_the_latest_error_trace_without_its_payload(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()

            server.emit_log("gemini", "connexion refusée", level="info")
            assert (await server.status())["health"]["last_error"] is None

            server.emit_log("gemini", "connexion perdue", level="error")

            last_error = (await server.status())["health"]["last_error"]
            assert last_error is not None
            assert last_error["source"] == "logging"
            assert last_error["kind"] == "gemini"
            assert last_error["level"] == "error"
            # Le message d'une Trace n'est jamais republié par le bandeau.
            assert set(last_error) == {"timestamp", "source", "kind", "level"}
            assert "connexion perdue" not in json.dumps(last_error)

    asyncio.run(scenario())


def test_the_trace_buffer_bound_is_visible_and_its_size_grows(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()

            before = (await server.status())["health"]["trace_buffer"]
            server.emit_log("diagnostic", "une trace de plus", level="warning")
            after = (await server.status())["health"]["trace_buffer"]

            assert after["capacity"] == 500
            assert after["size"] >= before["size"] + 1

    asyncio.run(scenario())


def test_the_diagnostic_stays_readable_while_the_installation_is_incomplete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as server:
            http = await server.install()

            snapshot = await server.status()
            installation = snapshot["installation"]

            assert installation["account_exists"] is True
            assert installation["gemini_configured"] is False
            assert installation["complete"] is False
            assert snapshot["restart_required"] is False
            # Sans clé, aucune surface d'exécution ne tourne.
            assert installation["surfaces"] == {
                "gemini": False,
                "transport": False,
                "webhook": False,
            }
            # Le bandeau reste renseigné : version, bases, Tampon de traces.
            health = snapshot["health"]
            assert health["version"] == __version__
            assert health["trace_buffer"]["capacity"] == 500
            assert health["databases"]["auth"] > 0

            # Une clé enregistrée après le démarrage termine l'Installation et
            # ouvre les surfaces à chaud : plus rien n'attend un redémarrage.
            async with http.put(
                f"{server.url}{SECRETS_PATH}/GEMINI_API_KEY",
                json={"value": GEMINI_KEY},
            ) as response:
                assert response.status == 200, await response.text()

            snapshot = await server.status()
            assert snapshot["restart_required"] is False
            assert snapshot["installation"]["gemini_configured"] is True
            assert snapshot["installation"]["complete"] is True
            assert snapshot["installation"]["surfaces"] == {
                "gemini": True,
                "transport": True,
                "webhook": False,
            }
            assert GEMINI_KEY not in json.dumps(snapshot, ensure_ascii=False)

    asyncio.run(scenario())


def test_status_requires_a_console_ticket(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()

            async with server.anonymous() as http:
                async with http.get(f"{server.url}{STATUS_PATH}") as response:
                    assert response.status == 401
                    assert (await response.json())["error"] == "unauthorized"

    asyncio.run(scenario())
