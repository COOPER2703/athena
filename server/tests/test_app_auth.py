"""Câblage réel : la production est fermée et l'Installation ne demande aucun code.

Le harnais démarre le vrai serveur (racine de composition + Console) sur des
ports éphémères. ``setup_required`` ne veut dire qu'une chose : aucun Compte.
Une fois le premier visiteur installé, la route d'installation est fermée pour
de bon et aucun Code d'installation n'apparaît jamais dans le journal
(ADR-0026).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness import TEST_PASSWORD
from server.adapters.console.contract import (
    LOGOUT_ALL_PATH,
    PASSWORD_PATH,
    SESSION_PATH,
)
from server_harness import running_server

NEW_PASSWORD = "nouveau mot de passe solide"


def test_the_production_console_installs_without_a_code_then_stays_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            # Avant installation, aucune route d'API n'est utilisable que par
            # les routes publiques d'identité et d'installation.
            async with server.console() as http:
                async with http.get(f"{server.url}/api/status") as response:
                    assert response.status == 409
                    assert (await response.json())["error"] == "setup_required"

                async with http.post(
                    f"{server.url}/api/setup",
                    json={"username": "admin", "password": "correct horse battery staple"},
                ) as response:
                    assert response.status == 200, await response.text()
                    assert (await response.json())["master_key"]

            # Plus jamais de Code d'installation dans le journal.
            assert "InstallationCode" not in server.read_log()

            # Un autre navigateur, sans Ticket, n'obtient que du JSON 401.
            async with server.anonymous() as anonymous:
                async with anonymous.get(f"{server.url}/api/status") as response:
                    assert response.status == 401
                    assert (await response.json())["error"] == "unauthorized"

    asyncio.run(scenario())


def test_a_restart_with_an_account_never_reopens_the_setup_route(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            await server.install()
            assert "InstallationCode" not in server.read_log()

        # Redémarrage sur la même base d'auth, avec un journal neuf.
        log_file = server.paths.log_file
        log_file.unlink()
        async with running_server(tmp_path) as restarted:
            assert "InstallationCode" not in restarted.read_log()

            # L'instance est installée : l'API exige un Ticket Console et la
            # route d'installation refuse, même sans code à présenter.
            async with restarted.console() as http:
                async with http.get(f"{restarted.url}/api/status") as response:
                    assert response.status == 401
                    assert (await response.json())["error"] == "unauthorized"
                async with http.post(
                    f"{restarted.url}/api/setup",
                    json={
                        "username": "intruder",
                        "password": "correct horse battery staple",
                    },
                ) as response:
                    assert response.status == 409
                    assert (
                        await response.json()
                    )["error"] == "already_installed"

    asyncio.run(scenario())


def test_the_account_can_change_its_password_and_disconnect_everywhere(
    tmp_path: Path,
) -> None:
    """Stories 15 et 16 : le Compte gère son mot de passe et ses navigateurs."""

    async def scenario() -> None:
        async with running_server(tmp_path) as server:
            first = await server.install()
            second = server.console()
            async with second.post(
                f"{server.url}/api/login",
                json={"username": "admin", "password": TEST_PASSWORD},
            ) as response:
                assert response.status == 200, await response.text()

            # Mauvais mot de passe actuel : refus stable, Ticket conservé.
            async with first.post(
                f"{server.url}{PASSWORD_PATH}",
                json={
                    "current_password": "pas le bon",
                    "new_password": NEW_PASSWORD,
                },
            ) as response:
                assert response.status == 403
                assert (await response.json())["error"] == "invalid_password"

            # Nouveau mot de passe trop court : le serveur applique sa règle.
            async with first.post(
                f"{server.url}{PASSWORD_PATH}",
                json={"current_password": TEST_PASSWORD, "new_password": "court"},
            ) as response:
                assert response.status == 400
                assert (await response.json())["error"] == "weak_password"

            # Changement réussi : les autres navigateurs sont révoqués.
            async with first.post(
                f"{server.url}{PASSWORD_PATH}",
                json={
                    "current_password": TEST_PASSWORD,
                    "new_password": NEW_PASSWORD,
                },
            ) as response:
                assert response.status == 200
                assert (await response.json())["changed"] is True

            async with second.get(f"{server.url}{SESSION_PATH}") as response:
                assert (await response.json())["authenticated"] is False
            async with first.get(f"{server.url}{SESSION_PATH}") as response:
                assert (await response.json())["authenticated"] is True

            # L'ancien mot de passe ne vaut plus, le nouveau ouvre un Ticket.
            async with server.console() as fresh:
                async with fresh.post(
                    f"{server.url}/api/login",
                    json={"username": "admin", "password": TEST_PASSWORD},
                ) as response:
                    assert response.status == 401
                async with fresh.post(
                    f"{server.url}/api/login",
                    json={"username": "admin", "password": NEW_PASSWORD},
                ) as response:
                    assert response.status == 200

            # « Déconnecter partout » révoque le Ticket courant lui-même.
            async with first.post(f"{server.url}{LOGOUT_ALL_PATH}") as response:
                assert response.status == 200
                assert (await response.json())["authenticated"] is False
            async with first.get(f"{server.url}{SESSION_PATH}") as response:
                assert (await response.json())["authenticated"] is False
            async with second.get(f"{server.url}{SESSION_PATH}") as response:
                assert (await response.json())["authenticated"] is False

    asyncio.run(scenario())
