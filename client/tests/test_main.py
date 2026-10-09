from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from client.__main__ import _build_app, cli, main
from client.app import ClientApp
from client.config import AppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_build_app_returns_client_app() -> None:
    app = _build_app(AppConfig())
    assert isinstance(app, ClientApp)


def test_entrypoints_are_exposed() -> None:
    assert callable(main)
    assert callable(cli)


def test_main_module_imports_without_pyaudio() -> None:
    code = (
        "import sys; "
        "sys.modules['pyaudio'] = None; "
        "import client.__main__; "
        "print('ok')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
