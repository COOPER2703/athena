"""Câblage réel : la production est fermée et le Code d'installation est publié.

Le harnais démarre le vrai serveur (racine de composition + Console) sur des
ports éphémères. ``install()`` lit le Code d'installation dans le journal
(``server.log``, le même que ``stderr``) et s'en sert pour installer l'instance.
Un second démarrage sur la même base ne doit plus jamais publier de code.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from server_harness import running_server

_CODE_LINE = re.compile(r"InstallationCode .*?\{\"code\": \"([^\"]+)\"}")


def _installation_code(journal: str) -> str:
    match = _CODE_LINE.search(journal)
    assert match is not None, journal
    return match.group(1)


def test_the_production_console_installs_from_the_journal_then_stays_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            # Avant installation, aucune route d'API n'est utilisable.
            async with server.console() as http:
                async with http.get(f"{server.url}/api/status") as response:
                    assert response.status == 409
                    assert (await response.json())["error"] == "setup_required"

                async with http.post(
                    f"{server.url}/api/setup",
                    json={
                        "code": "faux-code",
                        "username": "admin",
                        "password": "correct horse battery staple",
                    },
                ) as response:
                    assert response.status == 403

            await server.install()

            # Le journal ne contient le code qu'une fois, au démarrage.
            journal = server.read_log()
            assert journal.count("InstallationCode") == 1
            assert _installation_code(journal)

            # Un autre navigateur, sans Ticket, n'obtient que du JSON 401.
            async with server.anonymous() as anonymous:
                async with anonymous.get(f"{server.url}/api/status") as response:
                    assert response.status == 401
                    assert (await response.json())["error"] == "unauthorized"

    asyncio.run(scenario())


def test_a_restart_with_an_account_never_publishes_an_installation_code(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            log_file = server.paths.log_file
            assert "InstallationCode" in server.read_log()

        # Redémarrage sur la même base d'auth, avec un journal neuf : le code
        # n'est jamais réémis dès qu'un Compte existe.
        log_file.unlink()
        async with running_server(tmp_path) as restarted:
            assert "InstallationCode" not in restarted.read_log()

            # L'instance est installée : l'API exige un Ticket Console.
            async with restarted.anonymous() as http:
                async with http.get(f"{restarted.url}/api/status") as response:
                    assert response.status == 401
                    assert (await response.json())["error"] == "unauthorized"

    asyncio.run(scenario())


def test_the_installation_code_line_is_json_parseable() -> None:
    """Le journal reste lisible par un outil : le code est un champ, pas du texte."""
    line = (
        "2026-01-01T00:00:00.000+00:00 INFO     [auth] InstallationCode "
        '{"code": "abc-123"}'
    )
    assert _installation_code(line) == "abc-123"
    payload: dict[str, Any] = json.loads(line.split("InstallationCode ", 1)[1])
    assert payload == {"code": "abc-123"}
