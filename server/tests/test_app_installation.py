"""Installation sans code, magasin de Secrets et surfaces fermées (ticket #170).

Tout passe par le vrai serveur du harnais : vraies requêtes HTTP, vrais
répertoires. Les scénarios observent ce qu'un navigateur ou un opérateur peut
voir — jamais une valeur de Secret, jamais un détail de câblage.
"""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest
import websockets

from conftest import TEST_PASSWORD
from server.adapters.console.contract import (
    CHAT_PATH,
    SECRETS_PATH,
    SESSION_PATH,
    SETUP_PATH,
    STATUS_PATH,
)
from server_harness import running_server

GEMINI_KEY = "cle-gemini-de-test-4b7c"
OTHER_KEY = "cle-gemini-remplacee-91ad"


def _secret_path(name: str) -> str:
    return f"{SECRETS_PATH}/{name}"


async def _refused(connect) -> None:
    """Prouve qu'un port n'écoute pas, sans attendre le délai de connexion."""
    with pytest.raises(OSError):
        await asyncio.wait_for(connect(), timeout=2.0)


def test_a_fresh_instance_starts_without_a_gemini_key_and_stays_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="", webhook=True) as server:
            async with server.anonymous() as http:
                async with http.get(f"{server.url}{SESSION_PATH}") as response:
                    assert response.status == 200
                    identity = await response.json()

            assert identity["v"] == 1
            assert identity["setup_required"] is True
            assert identity["authenticated"] is False
            assert identity["username"] is None
            assert identity["gemini_configured"] is False
            assert identity["installation_complete"] is False
            assert identity["restart_required"] is False
            assert identity["secrets_unreadable"] is False

            # Aucune surface d'exécution n'écoute : ni transport, ni Webhook.
            await _refused(
                lambda: websockets.connect(server.transport_url)
            )
            await _refused(
                lambda: asyncio.open_connection("127.0.0.1", server.webhook_port)
            )

    asyncio.run(scenario())


def test_setup_creates_the_only_account_without_a_code_and_shows_the_key_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as server:
            async with server.console() as http:
                async with http.post(
                    f"{server.url}{SETUP_PATH}",
                    json={"username": "admin", "password": TEST_PASSWORD},
                ) as response:
                    assert response.status == 200, await response.text()
                    body = await response.json()

                assert body["authenticated"] is True
                assert body["username"] == "admin"
                master_key = body["master_key"]
                assert isinstance(master_key, str) and master_key

                # Le fichier de clé est séparé du magasin et lisible du seul
                # propriétaire ; l'API ne le renverra plus jamais.
                key_file = server.paths.master_key
                assert key_file.is_file()
                assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
                assert key_file.read_text(encoding="utf-8").strip() == master_key

                # Une seconde installation est refusée pour de bon, même depuis
                # un navigateur sans Ticket.
                async with server.console() as other:
                    async with other.post(
                        f"{server.url}{SETUP_PATH}",
                        json={"username": "intruder", "password": TEST_PASSWORD},
                    ) as response:
                        assert response.status == 409
                        assert (
                            await response.json()
                        )["error"] == "already_installed"

                async with http.get(f"{server.url}{SESSION_PATH}") as response:
                    identity = await response.json()
                assert identity["setup_required"] is False
                assert identity["authenticated"] is True
                assert identity["gemini_configured"] is False
                assert identity["installation_complete"] is False

    asyncio.run(scenario())


def test_the_diagnostic_answers_with_an_account_but_no_key(tmp_path: Path) -> None:
    """``setup_required`` ne vaut que « aucun Compte » : le reste répond (ADR-0025)."""

    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as server:
            http = await server.install()

            async with http.get(f"{server.url}{STATUS_PATH}") as response:
                assert response.status == 200
                assert (await response.json())["session"]["state"] == "Idle"

            async with http.get(f"{server.url}{SECRETS_PATH}") as response:
                assert response.status == 200
                body = await response.json()
            assert body["secrets_unreadable"] is False
            assert body["secrets"] == [
                {
                    "name": "GEMINI_API_KEY",
                    "configured": False,
                    "updated_at": None,
                }
            ]

    asyncio.run(scenario())


def test_the_conversation_is_refused_until_the_key_is_configured(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as server:
            http = await server.install()

            async with http.post(
                f"{server.url}{CHAT_PATH}", json={"text": "bonjour"}
            ) as response:
                assert response.status == 409
                body = await response.json()
            assert body["error"] == "installation_incomplete"
            assert "GEMINI_API_KEY" in body["fields"]

    asyncio.run(scenario())


def test_an_invalid_key_is_a_field_error_and_stores_nothing(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(
            tmp_path, gemini_key="", secret_validator=_rejecting_validator()
        ) as server:
            http = await server.install()

            async with http.put(
                f"{server.url}{_secret_path('GEMINI_API_KEY')}",
                json={"value": "cle-refusee"},
            ) as response:
                assert response.status == 400
                body = await response.json()
            assert body["error"] == "invalid_secret"
            assert body["fields"]["value"]

            async with http.get(f"{server.url}{SECRETS_PATH}") as response:
                listed = await response.json()
            assert listed["secrets"][0]["configured"] is False

            async with http.get(f"{server.url}{SESSION_PATH}") as response:
                identity = await response.json()
            assert identity["gemini_configured"] is False
            assert identity["installation_complete"] is False

    asyncio.run(scenario())


class _RejectingValidator:
    async def validate(self, name: str, value: str) -> str | None:
        return "Clé refusée par le service."


def _rejecting_validator() -> _RejectingValidator:
    return _RejectingValidator()


def test_a_valid_key_completes_the_installation_and_is_never_read_back(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as server:
            http = await server.install()

            async with http.put(
                f"{server.url}{_secret_path('GEMINI_API_KEY')}",
                json={"value": GEMINI_KEY},
            ) as response:
                assert response.status == 200, await response.text()
                stored = await response.json()
            assert stored["secret"]["name"] == "GEMINI_API_KEY"
            assert stored["secret"]["configured"] is True
            assert stored["secret"]["updated_at"] is not None

            async with http.get(f"{server.url}{SECRETS_PATH}") as response:
                body = await response.json()
                assert GEMINI_KEY not in await response.text()
            assert body["secrets"][0]["configured"] is True
            assert body["secrets_unreadable"] is False

            async with http.get(f"{server.url}{SESSION_PATH}") as response:
                identity = await response.json()
            assert identity["gemini_configured"] is True
            assert identity["installation_complete"] is True
            # La clé est enregistrée mais le processus a démarré sans elle.
            assert identity["restart_required"] is True

            # Ni l'instantané, ni le journal, ni le flux de traces ne la
            # reprennent.
            async with http.get(f"{server.url}{STATUS_PATH}") as response:
                assert GEMINI_KEY not in await response.text()
            assert GEMINI_KEY not in server.read_log()
            async with server.trace_stream() as stream:
                assert all(
                    GEMINI_KEY not in str(trace) for trace in stream.backlog
                )

            # La base ne contient pas la valeur en clair.
            raw = server.paths.settings_db.read_bytes()
            assert GEMINI_KEY.encode() not in raw

            # Effacer la clé rend l'Installation de nouveau incomplète.
            async with http.delete(
                f"{server.url}{_secret_path('GEMINI_API_KEY')}"
            ) as response:
                assert response.status == 200
            async with http.get(f"{server.url}{SESSION_PATH}") as response:
                identity = await response.json()
            assert identity["gemini_configured"] is False
            assert identity["installation_complete"] is False

    asyncio.run(scenario())


def test_the_validator_receives_the_name_and_value_before_storage(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as server:
            http = await server.install()
            async with http.put(
                f"{server.url}{_secret_path('GEMINI_API_KEY')}",
                json={"value": OTHER_KEY},
            ) as response:
                assert response.status == 200

            assert server.validator.seen == [("GEMINI_API_KEY", OTHER_KEY)]

    asyncio.run(scenario())


def test_a_key_from_the_process_environment_opens_the_surfaces(
    tmp_path: Path,
) -> None:
    """La configuration effective se remplace en test sans relancer de processus."""

    async def scenario() -> None:
        async with running_server(tmp_path, env={"GEMINI_API_KEY": "cle-env"}) as server:
            async with server.anonymous() as http:
                async with http.get(f"{server.url}{SESSION_PATH}") as response:
                    identity = await response.json()
            assert identity["gemini_configured"] is True
            assert identity["restart_required"] is False

            http = await server.install()
            async with http.get(f"{server.url}{SESSION_PATH}") as response:
                identity = await response.json()
            assert identity["installation_complete"] is True

            # Le transport est bien ouvert : un Client peut s'inscrire.
            async with server.client():
                pass

    asyncio.run(scenario())


def test_a_lost_master_key_is_reported_and_secrets_can_be_entered_again(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, gemini_key="") as first:
            http = await first.install()
            async with http.put(
                f"{first.url}{_secret_path('GEMINI_API_KEY')}",
                json={"value": GEMINI_KEY},
            ) as response:
                assert response.status == 200

        # La clé maître disparaît avec une sauvegarde incomplète du magasin.
        first.paths.master_key.unlink()

        async with running_server(tmp_path, gemini_key="") as restarted:
            async with restarted.anonymous() as http:
                async with http.get(f"{restarted.url}{SESSION_PATH}") as response:
                    identity = await response.json()
            assert identity["setup_required"] is False
            assert identity["secrets_unreadable"] is True
            assert identity["gemini_configured"] is False
            assert identity["installation_complete"] is False

            # Aucune réinitialisation silencieuse : la ligne est toujours là.
            async with restarted.console() as http:
                async with http.post(
                    f"{restarted.url}/api/login",
                    json={"username": "admin", "password": TEST_PASSWORD},
                ) as response:
                    assert response.status == 200
                async with http.get(
                    f"{restarted.url}{SECRETS_PATH}"
                ) as response:
                    listed = await response.json()
                assert listed["secrets"][0]["configured"] is True

                # Re-saisir la clé répare l'instance, avec une nouvelle clé
                # maître écrite en 0600 sur l'hôte.
                async with http.put(
                    f"{restarted.url}{_secret_path('GEMINI_API_KEY')}",
                    json={"value": OTHER_KEY},
                ) as response:
                    assert response.status == 200
                async with http.get(
                    f"{restarted.url}{SESSION_PATH}"
                ) as response:
                    identity = await response.json()
            assert identity["secrets_unreadable"] is False
            assert identity["gemini_configured"] is True
            assert restarted.paths.master_key.is_file()
            assert (
                stat.S_IMODE(restarted.paths.master_key.stat().st_mode) == 0o600
            )

    asyncio.run(scenario())
