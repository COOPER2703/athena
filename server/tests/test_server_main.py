from __future__ import annotations

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


def test_python_m_server_starts_and_shuts_down_cleanly_on_sigint() -> None:
    env = {
        **os.environ,
        "GEMINI_API_KEY": "test-key",
        "WS_HOST": "127.0.0.1",
        "WS_PORT": str(_free_port()),
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
        deadline = time.monotonic() + 5.0
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
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5.0)
