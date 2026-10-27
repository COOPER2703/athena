"""Câblage réel : la production est fermée et le Code d'installation est publié.

Le scénario démarre ``build_app`` comme le vrai serveur, lit le Code
d'installation dans le journal (``server.log``, le même que ``stderr``), s'en
sert pour installer l'instance, puis vérifie que l'API est close sans Ticket.
Un second démarrage sur la même base ne doit plus jamais publier de code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from pathlib import Path
from typing import Any

import aiohttp

from server.app.config import (
    AppConfig,
    AuthConfig,
    ConsoleConfig,
    GeminiConfig,
    PluginsConfig,
    ServerConfig,
    WebhookConfig,
)
from server.app.root import build_app

PASSWORD = "correct horse battery staple"
_CODE_LINE = re.compile(r"InstallationCode .*?\{\"code\": \"([^\"]+)\"}")


class _FakeMemory:
    """Mémoire factice : ``build_app`` ne charge ni modèle ni base réelle."""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _config(tmp_path: Path, *, console_port: int, log_dir: Path) -> AppConfig:
    return AppConfig(
        gemini=GeminiConfig(api_key="test-key"),
        server=ServerConfig(host="127.0.0.1", port=_free_port()),
        console=ConsoleConfig(port=console_port),
        webhook=WebhookConfig(enabled=False),
        auth=AuthConfig(db_path=str(tmp_path / "auth.db")),
        plugins=PluginsConfig(
            directory=str(tmp_path / "plugins"),
            state_path=str(tmp_path / "plugins.json"),
        ),
        log_dir=str(log_dir),
    )


async def _wait_until_ready(http: aiohttp.ClientSession, url: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while True:
        try:
            async with http.get(f"{url}/healthz") as response:
                if response.status == 200:
                    return
        except aiohttp.ClientConnectorError:
            pass
        if loop.time() > deadline:
            raise AssertionError("la Console n'a pas démarré")
        await asyncio.sleep(0.01)


async def _wait_for_log(path: Path, needle: str) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5.0
    while True:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if needle in text:
                return text
        if loop.time() > deadline:
            raise AssertionError(f"{needle!r} absent du journal {path}")
        await asyncio.sleep(0.01)


def _installation_code(journal: str) -> str:
    match = _CODE_LINE.search(journal)
    assert match is not None, journal
    return match.group(1)


def test_the_production_console_installs_from_the_journal_then_stays_closed(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            "server.app.root.SentenceTransformerEmbedder", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "server.app.root.SqliteMemoryStore", lambda *a, **k: _FakeMemory()
        )
        port = _free_port()
        logs = tmp_path / "logs"
        app = build_app(
            _config(tmp_path, console_port=port, log_dir=logs),
            secrets=None,
            env={},
            env_file=None,
        )
        root_logger = logging.getLogger()
        handlers = root_logger.handlers[:]
        level = root_logger.level
        runner = asyncio.create_task(app.run())
        url = f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True, treat_as_secure_origin=url)
            ) as http:
                await _wait_until_ready(http, url)
                journal = await _wait_for_log(logs / "server.log", "InstallationCode")
                code = _installation_code(journal)

                # Avant installation, aucune route d'API n'est utilisable.
                async with http.get(f"{url}/api/status") as response:
                    assert response.status == 409
                    assert (await response.json())["error"] == "setup_required"

                async with http.post(
                    f"{url}/api/setup",
                    json={
                        "code": code,
                        "username": "admin",
                        "password": PASSWORD,
                    },
                ) as response:
                    assert response.status == 200
                    assert (await response.json())["authenticated"] is True

                # Le journal ne contient le code qu'une fois, au démarrage.
                journal = (logs / "server.log").read_text(encoding="utf-8")
                assert journal.count("InstallationCode") == 1

            # Un autre navigateur, sans Ticket, n'obtient que du JSON 401.
            async with aiohttp.ClientSession() as anonymous:
                async with anonymous.get(f"{url}/api/status") as response:
                    assert response.status == 401
                    assert (await response.json())["error"] == "unauthorized"
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 5.0)
            root_logger.handlers[:] = handlers
            root_logger.setLevel(level)

    asyncio.run(scenario())


def test_a_restart_with_an_account_never_publishes_an_installation_code(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(
            "server.app.root.SentenceTransformerEmbedder", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "server.app.root.SqliteMemoryStore", lambda *a, **k: _FakeMemory()
        )
        root_logger = logging.getLogger()
        handlers = root_logger.handlers[:]
        level = root_logger.level

        first_port = _free_port()
        first_logs = tmp_path / "first-logs"
        app = build_app(
            _config(tmp_path, console_port=first_port, log_dir=first_logs),
            secrets=None,
            env={},
            env_file=None,
        )
        runner = asyncio.create_task(app.run())
        first_url = f"http://127.0.0.1:{first_port}"
        try:
            async with aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(
                    unsafe=True, treat_as_secure_origin=first_url
                )
            ) as http:
                await _wait_until_ready(http, first_url)
                journal = await _wait_for_log(
                    first_logs / "server.log", "InstallationCode"
                )
                async with http.post(
                    f"{first_url}/api/setup",
                    json={
                        "code": _installation_code(journal),
                        "username": "admin",
                        "password": PASSWORD,
                    },
                ) as response:
                    assert response.status == 200
        finally:
            app.request_shutdown()
            await asyncio.wait_for(runner, 5.0)

        second_port = _free_port()
        second_logs = tmp_path / "second-logs"
        restarted = build_app(
            _config(tmp_path, console_port=second_port, log_dir=second_logs),
            secrets=None,
            env={},
            env_file=None,
        )
        runner = asyncio.create_task(restarted.run())
        second_url = f"http://127.0.0.1:{second_port}"
        try:
            async with aiohttp.ClientSession() as http:
                await _wait_until_ready(http, second_url)
                journal = await _wait_for_log(second_logs / "server.log", "started")
                assert "InstallationCode" not in journal

                # L'instance est installée : l'API exige un Ticket Console.
                async with http.get(f"{second_url}/api/status") as response:
                    assert response.status == 401
                    assert (await response.json())["error"] == "unauthorized"
        finally:
            restarted.request_shutdown()
            await asyncio.wait_for(runner, 5.0)
            root_logger.handlers[:] = handlers
            root_logger.setLevel(level)

    asyncio.run(scenario())


def test_the_installation_code_line_is_json_parseable(tmp_path: Path) -> None:
    """Le journal reste lisible par un outil : le code est un champ, pas du texte."""
    line = (
        "2026-01-01T00:00:00.000+00:00 INFO     [auth] InstallationCode "
        '{"code": "abc-123"}'
    )
    assert _installation_code(line) == "abc-123"
    payload: dict[str, Any] = json.loads(line.split("InstallationCode ", 1)[1])
    assert payload == {"code": "abc-123"}
