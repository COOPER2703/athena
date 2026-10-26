from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

from conftest import wait_until
from server.app.config import (
    AppConfig,
    ConsoleConfig,
    GeminiConfig,
    MemoryConfig,
    PluginsConfig,
    ServerConfig,
    WebhookConfig,
)
from server.app.root import build_app
from server.app.secrets import EnvSecretResolver, MissingSecretError


def test_resolver_returns_value_from_env_mapping() -> None:
    resolver = EnvSecretResolver({"GEMINI_API_KEY": "abc"})
    assert resolver.resolve("GEMINI_API_KEY") == "abc"


def test_resolver_raises_clear_error_when_missing() -> None:
    resolver = EnvSecretResolver({})
    with pytest.raises(MissingSecretError) as excinfo:
        resolver.resolve("GEMINI_API_KEY")
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_resolver_treats_empty_value_as_missing() -> None:
    resolver = EnvSecretResolver({"GEMINI_API_KEY": ""})
    with pytest.raises(MissingSecretError):
        resolver.resolve("GEMINI_API_KEY")


def test_env_resolver_defaults_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_TEST_SECRET", "s3cr3t")
    assert EnvSecretResolver().resolve("ATHENA_TEST_SECRET") == "s3cr3t"


PLUGIN_TEMPLATE = '''
from plugin_api import PluginBinding, ToolBinding, ToolSpec


async def handle(arguments):
    return {{"status": "ok"}}


def create(context):
    # Le nom du Tool révèle la valeur résolue sans jamais la tracer.
    return PluginBinding(
        name={name!r},
        tools=(
            ToolBinding(
                spec=ToolSpec(
                    name=f"{{context.name}}__{{context.secrets['token']}}",
                    description="source du secret",
                    parameters={{}},
                ),
                handler=handle,
            ),
        ),
    )
'''


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _install_secret_plugin(root: Path, name: str) -> None:
    directory = root / "plugins" / name
    directory.mkdir(parents=True)
    (directory / "plugin.json").write_text(
        json.dumps(
            {
                "api_version": 1,
                "name": name,
                "version": "0.1.0",
                "description": "Plugin à secret",
                "platform": "server",
                "entrypoint": "plugin.py",
                "secrets": [{"key": "token", "required": True}],
            }
        ),
        encoding="utf-8",
    )
    (directory / "plugin.py").write_text(
        PLUGIN_TEMPLATE.format(name=name), encoding="utf-8"
    )


def test_build_app_resolves_plugin_secrets_from_the_dotenv_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Les secrets de plugin voient le ``.env``, comme le reste de la config."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ATHENA_PLUGIN_FICHIER_TOKEN", raising=False)
    monkeypatch.setenv("ATHENA_PLUGIN_ENV_TOKEN", "du-processus")
    (tmp_path / ".env").write_text(
        "ATHENA_PLUGIN_FICHIER_TOKEN=du-fichier\n"
        "ATHENA_PLUGIN_ENV_TOKEN=ecrasee-par-le-processus\n",
        encoding="utf-8",
    )
    _install_secret_plugin(tmp_path, "fichier")
    _install_secret_plugin(tmp_path, "env")
    (tmp_path / "plugins.json").write_text(
        json.dumps({"enabled": ["fichier", "env"], "config": {}}), encoding="utf-8"
    )
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=_free_port()),
        gemini=GeminiConfig(api_key="key"),
        memory=MemoryConfig(db_path=str(tmp_path / "memory.db")),
        webhook=WebhookConfig(enabled=False),
        console=ConsoleConfig(enabled=False),
        plugins=PluginsConfig(
            directory=str(tmp_path / "plugins"),
            state_path=str(tmp_path / "plugins.json"),
        ),
        log_dir=str(tmp_path / "logs"),
    )
    app = build_app(config)

    async def scenario() -> None:
        runner = asyncio.create_task(app.run())
        try:
            await wait_until(
                lambda: app.tools.owner_of("fichier__du-fichier") == "fichier"
                and app.tools.owner_of("env__du-processus") == "env",
                timeout=10.0,
            )
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 30.0)

    asyncio.run(scenario())
