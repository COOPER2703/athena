"""Zone Plugins : état, activation, configuration et secrets (ticket #176).

Ces tests parlent au vrai serveur du harnais : vraies requêtes HTTP, vrai
fichier d'état, vrai magasin de Secrets. Ils prouvent les contrats observables
— ce que la Console reçoit, ce qui est écrit sur disque, et ce qui prend effet
au redémarrage — sans inspecter le câblage interne.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from server.adapters.console.contract import PLUGINS_PATH, STATUS_PATH
from server.adapters.plugins.loader import secret_variable
from server_harness import (
    SECRET_CHECKING_CODE,
    plugin_manifest,
    running_server,
    write_plugin,
    write_plugins_state,
)

_TOKEN_SECRET = [{"key": "token", "required": False}]
_BASE_URL_CONFIG = [{"key": "base_url", "type": "url", "required": True}]


def _prepare_alpha(paths) -> None:
    """Un Plugin serveur activé, avec une URL et un secret requis."""
    write_plugin(
        paths.plugins_dir,
        "alpha",
        manifest=plugin_manifest(
            "alpha",
            version="1.2.3",
            description="Alpha sait répondre.",
            config=_BASE_URL_CONFIG,
            secrets=_TOKEN_SECRET,
        ),
    )
    write_plugins_state(
        paths.plugins_state,
        enabled=("alpha",),
        config={"alpha": {"base_url": "https://alpha.test"}},
    )


def _prepare_alpha_with_secret(expected: str):
    """Fabrique un ``prepare`` dont le Plugin vérifie la valeur de son secret."""

    def prepare(paths) -> None:
        write_plugin(
            paths.plugins_dir,
            "alpha",
            manifest=plugin_manifest(
                "alpha",
                secrets=[{"key": "token", "required": False}],
            ),
            code=SECRET_CHECKING_CODE.format(expected=expected),
        )
        write_plugins_state(paths.plugins_state, enabled=("alpha",))

    return prepare


def test_the_plugins_route_lists_state_version_platform_and_tools(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(
            tmp_path,
            prepare=_prepare_alpha,
            env={"ATHENA_PLUGIN_ALPHA_TOKEN": "top-secret"},
        ) as server:
            http = await server.install()
            async with http.get(f"{server.url}{PLUGINS_PATH}") as response:
                assert response.status == 200
                body = await response.json()

            assert body["v"] == 1
            assert body["restart_required"] is False
            [plugin] = body["plugins"]
            assert plugin["name"] == "alpha"
            assert plugin["enabled"] is True
            assert plugin["state"] == "activated"
            assert plugin["version"] == "1.2.3"
            assert plugin["platform"] == "server"
            assert plugin["description"] == "Alpha sait répondre."
            assert plugin["tools"] == ["alpha__echo"]
            assert "alpha__echo" in plugin["instruction"]
            assert plugin["pending_restart"] is False
            assert plugin["error"] == ""
            # La configuration non secrète est lisible, elle.
            assert plugin["config"] == [
                {
                    "key": "base_url",
                    "type": "url",
                    "required": True,
                    "value": "https://alpha.test",
                }
            ]
            # Le secret n'expose que le nom de la variable attendue.
            assert plugin["secrets"] == [
                {
                    "key": "token",
                    "required": False,
                    "variable": secret_variable("alpha", "token"),
                    "configured": False,
                    "updated_at": None,
                }
            ]
            assert "top-secret" not in json.dumps(body)

    asyncio.run(scenario())


def test_a_broken_or_unlisted_plugin_is_listed_without_failing_the_startup(
    tmp_path: Path,
) -> None:
    def prepare(paths) -> None:
        broken = paths.plugins_dir / "casse"
        broken.mkdir(parents=True)
        (broken / "plugin.json").write_text("{ pas du JSON", encoding="utf-8")
        write_plugin(paths.plugins_dir, "alpha")
        # alpha existe mais n'est pas listé : le serveur le montre inactif.

    async def scenario() -> None:
        async with running_server(tmp_path, prepare=prepare) as server:
            http = await server.install()
            async with http.get(f"{server.url}{PLUGINS_PATH}") as response:
                assert response.status == 200
                body = await response.json()

            by_name = {plugin["name"]: plugin for plugin in body["plugins"]}
            assert by_name["alpha"]["enabled"] is False
            assert by_name["alpha"]["state"] == "disabled"
            assert by_name["alpha"]["version"] == "0.1.0"
            # Le dossier cassé reste lisible, avec une raison, et le serveur a
            # démarré normalement.
            assert by_name["casse"]["state"] == "disabled"
            assert by_name["casse"]["version"] == ""
            assert "not valid JSON" in by_name["casse"]["error"]

            # L'instantané d'observation continue de répondre.
            async with http.get(f"{server.url}{STATUS_PATH}") as response:
                assert response.status == 200

    asyncio.run(scenario())


def test_a_missing_plugin_stays_listed_and_can_be_deactivated(
    tmp_path: Path,
) -> None:
    def prepare(paths) -> None:
        write_plugins_state(paths.plugins_state, enabled=("fantome",))

    async def scenario() -> None:
        async with running_server(tmp_path, prepare=prepare) as server:
            http = await server.install()
            async with http.get(f"{server.url}{PLUGINS_PATH}") as response:
                body = await response.json()
            [plugin] = body["plugins"]
            assert plugin["name"] == "fantome"
            assert plugin["enabled"] is True
            assert plugin["state"] == "missing"
            assert plugin["version"] == ""

            # L'activation est réparable sans manifeste : on peut nettoyer la
            # ligne d'état d'un dossier disparu.
            async with http.put(
                f"{server.url}{PLUGINS_PATH}/fantome", json={"enabled": False}
            ) as response:
                assert response.status == 200
                body = await response.json()
            assert body["plugin"]["enabled"] is False
            assert server.plugins_state()["enabled"] == []

    asyncio.run(scenario())


def test_activation_is_persisted_and_takes_effect_at_the_next_start(
    tmp_path: Path,
) -> None:
    def prepare(paths) -> None:
        write_plugin(paths.plugins_dir, "alpha")

    async def first_start() -> None:
        async with running_server(tmp_path, prepare=prepare) as server:
            http = await server.install()
            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha", json={"enabled": True}
            ) as response:
                assert response.status == 200
                body = await response.json()

            assert body["plugin"]["enabled"] is True
            assert body["plugin"]["state"] == "disabled"
            assert body["plugin"]["pending_restart"] is True
            assert body["restart_required"] is True
            assert server.plugins_state()["enabled"] == ["alpha"]
            # Rien n'est rechargé à chaud : le Tool n'existe pas encore.
            assert body["plugin"]["tools"] == []

    async def second_start() -> None:
        async with running_server(tmp_path) as server:
            http = await server.login()
            async with http.get(f"{server.url}{PLUGINS_PATH}") as response:
                body = await response.json()
            [plugin] = body["plugins"]
            assert plugin["enabled"] is True
            assert plugin["state"] == "activated"
            assert plugin["tools"] == ["alpha__echo"]
            assert plugin["pending_restart"] is False
            assert body["restart_required"] is False

    asyncio.run(first_start())
    asyncio.run(second_start())


def test_deactivation_preserves_unknown_keys_and_other_plugins(
    tmp_path: Path,
) -> None:
    def prepare(paths) -> None:
        write_plugin(paths.plugins_dir, "alpha")
        write_plugins_state(
            paths.plugins_state,
            enabled=("alpha",),
            config={
                "alpha": {"base_url": "https://alpha.test"},
                "autre": {"garde": "moi"},
            },
            extra={"custom": {"keep": True}},
        )

    async def scenario() -> None:
        async with running_server(tmp_path, prepare=prepare) as server:
            http = await server.install()
            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha", json={"enabled": False}
            ) as response:
                assert response.status == 200
                body = await response.json()
            assert body["plugin"]["enabled"] is False

            state = server.plugins_state()
            # Les clés inconnues et la configuration des autres Plugins
            # survivent à l'écriture.
            assert state["custom"] == {"keep": True}
            assert state["config"]["autre"] == {"garde": "moi"}
            assert state["config"]["alpha"] == {"base_url": "https://alpha.test"}
            assert state["enabled"] == []
            # Écriture atomique : aucun fichier temporaire ne reste.
            leftovers = [
                path.name
                for path in server.paths.plugins_state.parent.iterdir()
                if path.name.startswith(".plugins.json.")
            ]
            assert leftovers == []

    asyncio.run(scenario())


def test_configuration_is_validated_against_the_manifest(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, prepare=_prepare_alpha) as server:
            http = await server.install()
            before = server.paths.plugins_state.read_bytes()

            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha",
                json={
                    "config": {
                        "base_url": "pas-une-url",
                        "typo": "valeur",
                    }
                },
            ) as response:
                assert response.status == 400
                body = await response.json()
            assert body["error"] == "invalid_plugin_config"
            assert body["fields"] == {
                "base_url": "invalid_url",
                "typo": "unknown_field",
            }
            # Rien n'a été écrit.
            assert server.paths.plugins_state.read_bytes() == before

            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha",
                json={"config": {"base_url": "   "}},
            ) as response:
                assert response.status == 400
                body = await response.json()
            assert body["fields"] == {"base_url": "required"}

            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha",
                json={"config": {"base_url": "https://nouveau.test"}},
            ) as response:
                assert response.status == 200
                body = await response.json()
            assert body["plugin"]["config"][0]["value"] == "https://nouveau.test"
            assert body["plugin"]["pending_restart"] is True

            # Le fichier porte la valeur validée, prête pour le redémarrage.
            state = server.plugins_state()
            assert state["config"]["alpha"]["base_url"] == "https://nouveau.test"

    asyncio.run(scenario())


def test_a_plugin_secret_is_write_only_and_resolved_from_the_store(
    tmp_path: Path,
) -> None:
    secret = "s3cr3t-de-test"

    async def first_start() -> None:
        async with running_server(
            tmp_path, prepare=_prepare_alpha_with_secret(secret)
        ) as server:
            http = await server.install()
            async with http.get(f"{server.url}{PLUGINS_PATH}") as response:
                body = await response.json()
            # Sans secret, le plugin a échoué mais le serveur tourne.
            assert body["plugins"][0]["state"] == "failed"

            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha/secrets/token",
                json={"value": secret},
            ) as response:
                assert response.status == 200
                raw = await response.text()
                body = await response.json()
            # La valeur n'est jamais relue, ni dans la réponse, ni ailleurs.
            assert secret not in raw
            assert body["secret"]["key"] == "token"
            assert body["secret"]["required"] is False
            assert body["secret"]["variable"] == secret_variable("alpha", "token")
            assert body["secret"]["configured"] is True
            assert isinstance(body["secret"]["updated_at"], float)

            async with http.get(f"{server.url}{PLUGINS_PATH}") as response:
                listing = await response.json()
            [secret_row] = listing["plugins"][0]["secrets"]
            assert secret_row["configured"] is True
            assert secret not in json.dumps(listing)

            async with http.get(f"{server.url}{STATUS_PATH}") as response:
                snapshot = await response.text()
            assert secret not in snapshot
            assert secret not in server.read_log()

    async def second_start() -> None:
        async with running_server(tmp_path) as server:
            http = await server.login()
            async with http.get(f"{server.url}{PLUGINS_PATH}") as response:
                body = await response.json()
            # Le magasin résout le secret au démarrage : le plugin s'active.
            assert body["plugins"][0]["state"] == "activated"

    asyncio.run(first_start())
    asyncio.run(second_start())


def test_the_environment_wins_over_the_store_for_a_plugin_secret(
    tmp_path: Path,
) -> None:
    async def first_start() -> None:
        async with running_server(
            tmp_path, prepare=_prepare_alpha_with_secret("env-value")
        ) as server:
            http = await server.install()
            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha/secrets/token",
                json={"value": "store-value"},
            ) as response:
                assert response.status == 200
                await response.json()

    async def second_start() -> None:
        async with running_server(
            tmp_path,
            env={"ATHENA_PLUGIN_ALPHA_TOKEN": "env-value"},
        ) as server:
            http = await server.login()
            async with http.get(f"{server.url}{PLUGINS_PATH}") as response:
                body = await response.json()
            # Si le magasin l'emportait, le plugin refuserait de démarrer.
            assert body["plugins"][0]["state"] == "activated"

    asyncio.run(first_start())
    asyncio.run(second_start())


def test_deleting_a_plugin_secret_makes_it_unconfigured(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, prepare=_prepare_alpha) as server:
            http = await server.install()
            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha/secrets/token",
                json={"value": "une-valeur"},
            ) as response:
                assert response.status == 200
                await response.json()

            async with http.delete(
                f"{server.url}{PLUGINS_PATH}/alpha/secrets/token"
            ) as response:
                assert response.status == 200
                body = await response.json()
            assert body["secret"]["configured"] is False
            assert body["secret"]["updated_at"] is None

            # Effacer deux fois n'est pas une erreur.
            async with http.delete(
                f"{server.url}{PLUGINS_PATH}/alpha/secrets/token"
            ) as response:
                assert response.status == 200

    asyncio.run(scenario())


def test_plugin_refusals_are_stable_codes_and_need_a_ticket(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, prepare=_prepare_alpha) as server:
            http = await server.install()
            async with server.anonymous() as anonymous:
                async with anonymous.get(f"{server.url}{PLUGINS_PATH}") as response:
                    assert response.status == 401

            async with http.put(
                f"{server.url}{PLUGINS_PATH}/inconnu", json={"enabled": True}
            ) as response:
                assert response.status == 404
                body = await response.json()
            assert body["error"] == "plugin_not_found"

            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha/secrets/inconnu",
                json={"value": "x"},
            ) as response:
                assert response.status == 404
                body = await response.json()
            assert body["error"] == "unknown_secret"

            async with http.put(
                f"{server.url}{PLUGINS_PATH}/alpha/secrets/token",
                json={"value": "   "},
            ) as response:
                assert response.status == 400
                body = await response.json()
            assert body["error"] == "invalid_plugin_secret"
            assert body["fields"] == {"value": "required"}

    asyncio.run(scenario())
