from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from server.__main__ import cli

REPO_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_cli_reports_a_missing_secret_with_a_clear_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli()

    assert excinfo.value.code == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_cli_reports_invalid_configuration_early(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("WS_PORT", "not-a-port")

    with pytest.raises(SystemExit) as excinfo:
        cli()

    assert excinfo.value.code == 2
    assert "port" in capsys.readouterr().err.lower()


def test_python_m_server_starts_and_shuts_down_cleanly_on_sigint(
    tmp_path: Path,
) -> None:
    env = {
        **os.environ,
        "GEMINI_API_KEY": "test-key",
        "WS_HOST": "127.0.0.1",
        "WS_PORT": str(_free_port()),
        "ATHENA_MEMORY_DB": str(tmp_path / "memory.db"),
        "ATHENA_LOG_DIR": str(tmp_path / "logs"),
        "ATHENA_CONSOLE_ENABLED": "0",
        # Le test vérifie le cycle de vie du serveur, pas le Webhook : ne pas
        # dépendre du port fixe 8766 (occupé par une instance de dev).
        "ATHENA_WEBHOOK_ENABLED": "0",
        "LOG_LEVEL": "INFO",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "server"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: list[str] = []

    def _drain() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            lines.append(line)

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    try:
        # Startup now warms the local embedding model before announcing
        # "started" (T2 memory), so allow a generous readiness budget.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if any("started" in line for line in lines):
                break
            time.sleep(0.02)

        assert process.poll() is None, f"server exited early: {''.join(lines)}"
        assert any("started" in line for line in lines), "".join(lines)

        process.send_signal(signal.SIGINT)
        process.wait(timeout=5.0)
        assert process.returncode == 0, "".join(lines)
        reader.join(timeout=1.0)
        assert any("stopped" in line for line in lines), "".join(lines)
        written = (tmp_path / "logs" / "server.log").read_text(encoding="utf-8")
        assert "started" in written
        assert "stopped" in written
        assert "DEBUG" not in written
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5.0)


def test_cli_reports_a_broken_plugin_and_exits_with_the_config_error_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("ATHENA_MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("ATHENA_LOG_DIR", str(tmp_path / "logs"))
    plugin_dir = tmp_path / "plugins" / "fixture"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "api_version": 2,
                "name": "fixture",
                "version": "0.1.0",
                "description": "Plugin cassé",
                "platform": "server",
                "entrypoint": "plugin.py",
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "plugin.py").write_text("", encoding="utf-8")
    (tmp_path / "plugins.json").write_text(
        json.dumps({"enabled": ["fixture"]}), encoding="utf-8"
    )

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        with pytest.raises(SystemExit) as excinfo:
            cli()
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert excinfo.value.code == 2
    error = capsys.readouterr().err
    assert "fixture" in error
    assert "api_version" in error


MANIFEST = {
    "api_version": 1,
    "name": "fixture",
    "version": "0.1.0",
    "description": "Plugin de fixture",
    "platform": "server",
    "entrypoint": "plugin.py",
}

EMPTY_PLUGIN_CODE = """
from plugin_api import PluginBinding

create = lambda context: PluginBinding(name="fixture", tools=())
"""


def _run_cli_with_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    manifest: dict | str | None = None,
    code: str = EMPTY_PLUGIN_CODE,
    state: dict | None = None,
    env: dict[str, str] | None = None,
    extra_plugins: tuple[tuple[str, dict, str], ...] = (),
) -> str:
    """Exécute le CLI sur des plugins de fixture et rend sa sortie d'erreur."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "key")
    monkeypatch.setenv("ATHENA_MEMORY_DB", str(tmp_path / "memory.db"))
    monkeypatch.setenv("ATHENA_LOG_DIR", str(tmp_path / "logs"))
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    payload = MANIFEST if manifest is None else manifest
    installed = [("fixture", payload, code), *extra_plugins]
    for name, plugin_manifest, plugin_code in installed:
        plugin_dir = tmp_path / "plugins" / name
        plugin_dir.mkdir(parents=True)
        if isinstance(plugin_manifest, str):
            (plugin_dir / "plugin.json").write_text(plugin_manifest, encoding="utf-8")
        else:
            (plugin_dir / "plugin.json").write_text(
                json.dumps(plugin_manifest, ensure_ascii=False), encoding="utf-8"
            )
        (plugin_dir / "plugin.py").write_text(plugin_code, encoding="utf-8")
    (tmp_path / "plugins.json").write_text(
        json.dumps(state or {"enabled": ["fixture"], "config": {}}),
        encoding="utf-8",
    )

    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        with pytest.raises(SystemExit) as excinfo:
            cli()
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)

    assert excinfo.value.code == 2
    error = capsys.readouterr().err
    # Un démarrage refusé ne trace jamais ``started``.
    assert "started" not in error
    log_file = tmp_path / "logs" / "server.log"
    if log_file.is_file():
        assert "started" not in log_file.read_text(encoding="utf-8")
    return error


def test_cli_names_the_plugin_and_the_missing_manifest_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = dict(MANIFEST)
    del manifest["description"]

    error = _run_cli_with_plugin(tmp_path, monkeypatch, capsys, manifest=manifest)

    assert "fixture" in error
    assert "description" in error


def test_cli_names_the_plugin_and_the_unreadable_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _run_cli_with_plugin(tmp_path, monkeypatch, capsys, manifest="{pas du json")

    assert "fixture" in error
    assert "JSON" in error


def test_cli_refuses_startup_when_a_required_secret_is_missing_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "valeur-secrete-a-ne-jamais-afficher"
    fixture_manifest = dict(MANIFEST, secrets=[{"key": "token", "required": True}])
    # Un premier plugin en échec retentable laisse une trace dans le flux, avec
    # sa raison expurgée ; le suivant, privé de secret, refuse le démarrage.
    bavard_manifest = dict(
        MANIFEST, name="bavard", secrets=[{"key": "token", "required": True}]
    )
    bavard_code = (
        "from plugin_api import PluginUnavailable\n"
        "def create(context):\n"
        f'    raise PluginUnavailable("refusé {{}}".format(context.secrets["token"]))\n'
    )

    error = _run_cli_with_plugin(
        tmp_path,
        monkeypatch,
        capsys,
        manifest=fixture_manifest,
        state={"enabled": ["bavard", "fixture"], "config": {}},
        env={"ATHENA_PLUGIN_BAVARD_TOKEN": secret},
        extra_plugins=(("bavard", bavard_manifest, bavard_code),),
    )

    assert "fixture" in error
    assert "ATHENA_PLUGIN_FIXTURE_TOKEN" in error
    assert secret not in error

    log_file = tmp_path / "logs" / "server.log"
    assert log_file.is_file()
    log = log_file.read_text(encoding="utf-8")
    assert secret not in log
    assert "***" in log


def test_cli_refuses_startup_when_a_required_config_key_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = dict(MANIFEST, config=[{"key": "url", "type": "url", "required": True}])

    error = _run_cli_with_plugin(tmp_path, monkeypatch, capsys, manifest=manifest)

    assert "fixture" in error
    assert "url" in error


def test_cli_reports_an_entrypoint_failing_at_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = _run_cli_with_plugin(
        tmp_path,
        monkeypatch,
        capsys,
        code='raise RuntimeError("dépendance absente à l\'import")',
    )

    assert "fixture" in error
    assert "dépendance absente à l'import" in error
