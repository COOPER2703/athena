from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from client.__main__ import _build_app, _enter_loop, cli, main
from client.app import ClientApp
from client.config import AppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeStdin:
    def __init__(self, lines: list[str]) -> None:
        self._lines = iter(lines)

    def readline(self) -> str:
        return next(self._lines, "")


class FailingApp:
    def __init__(self) -> None:
        self.taps = 0

    async def tap(self) -> None:
        self.taps += 1
        raise RuntimeError("transport down")


def test_build_app_returns_client_app() -> None:
    app = _build_app(AppConfig())
    assert isinstance(app, ClientApp)


def test_entrypoints_are_exposed() -> None:
    assert callable(main)
    assert callable(cli)


def test_enter_loop_survives_tap_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app = FailingApp()
    monkeypatch.setattr(sys, "stdin", FakeStdin(["\n", "\n", "\n"]))

    with caplog.at_level("ERROR", logger="athena.client"):
        asyncio.run(_enter_loop(app))

    assert app.taps == 3
    assert any(record.levelname == "ERROR" for record in caplog.records)


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
