from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import client.__main__ as main_module
from client.__main__ import _enter_loop, cli, main

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


def test_main_starts_the_page_daemon_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeDaemon:
        def __init__(self, cfg: object, store: object) -> None:
            events.append("init")

        async def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

        async def tap(self) -> None:
            return None

    monkeypatch.setattr(main_module, "ClientDaemon", FakeDaemon)
    monkeypatch.setattr(
        main_module, "SettingsStore", lambda path: SimpleNamespace(path=path)
    )
    monkeypatch.setattr(sys, "stdin", FakeStdin([]))

    stop = asyncio.Event()
    stop.set()
    asyncio.run(main(stop))

    assert events == ["init", "start", "stop"]


def test_cli_reports_an_unavailable_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing(shutdown: object = None) -> None:
        raise OSError("address already in use")

    monkeypatch.setattr(main_module, "main", failing)

    with pytest.raises(SystemExit) as excinfo:
        cli()

    assert excinfo.value.code == 2
    assert "Page de couplage indisponible" in capsys.readouterr().err


def test_entrypoints_are_exposed() -> None:
    assert callable(main)
    assert callable(cli)


def test_enter_loop_survives_tap_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    app = FailingApp()
    monkeypatch.setattr(sys, "stdin", FakeStdin(["\n", "\n", "\n"]))

    with caplog.at_level("ERROR", logger="athena.client"):
        asyncio.run(_enter_loop(app))  # type: ignore[arg-type]

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
