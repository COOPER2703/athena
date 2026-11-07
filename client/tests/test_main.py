from __future__ import annotations

import asyncio
import subprocess
import sys
import types
from pathlib import Path

import pytest

import client.__main__ as main_module
from client.__main__ import _build_app, _enter_loop, cli, main
from client.app import ClientApp
from client.config import AppConfig
from client.wake_word import WakeWordUnavailableError
from client.ws_client import MissingAccessTokenError

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


def test_build_app_returns_client_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATHENA_ACCESS_TOKEN", "ath_secret")
    monkeypatch.setenv("ATHENA_WAKE_WORD", "0")
    app = _build_app(AppConfig())
    assert isinstance(app, ClientApp)


def test_build_app_builds_the_detector_from_the_wake_word_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_ACCESS_TOKEN", "ath_secret")
    monkeypatch.setenv("ATHENA_WAKE_WORD", "1")
    calls: list[object] = []

    class RecordingDetector:
        @classmethod
        def from_config(cls, cfg: object) -> "RecordingDetector":
            calls.append(cfg)
            return cls()

        def process(self, chunk: bytes) -> None:
            return None

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_module, "OpenWakeWordDetector", RecordingDetector)
    cfg = AppConfig()

    _build_app(cfg)

    assert calls == [cfg.wake_word]


def test_build_app_refuses_to_start_without_an_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATHENA_ACCESS_TOKEN", raising=False)
    with pytest.raises(MissingAccessTokenError) as excinfo:
        _build_app(AppConfig())
    assert "ATHENA_ACCESS_TOKEN" in str(excinfo.value)


def test_cli_reports_a_missing_access_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ATHENA_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        cli()
    assert excinfo.value.code == 2
    assert "ATHENA_ACCESS_TOKEN" in capsys.readouterr().err


def test_build_app_does_not_load_a_detector_when_wake_word_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATHENA_ACCESS_TOKEN", "ath_secret")
    monkeypatch.setenv("ATHENA_WAKE_WORD", "0")

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("la Veille coupée ne doit pas charger de modèle")

    monkeypatch.setattr(main_module, "OpenWakeWordDetector", fail)

    app = _build_app(AppConfig())

    assert isinstance(app, ClientApp)


def test_cli_reports_an_unavailable_wake_word(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ATHENA_ACCESS_TOKEN", "ath_secret")

    def fail(cfg: AppConfig) -> None:
        raise WakeWordUnavailableError(
            "Modèle de Wake word introuvable : /tmp/manquant.onnx."
        )

    monkeypatch.setattr(main_module, "_build_app", fail)

    with pytest.raises(SystemExit) as excinfo:
        cli()

    assert excinfo.value.code == 2
    assert "/tmp/manquant.onnx" in capsys.readouterr().err


def test_cli_exits_2_when_the_real_model_constructor_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    model = tmp_path / "athena.onnx"
    model.write_bytes(b"corrupted")
    monkeypatch.setenv("ATHENA_ACCESS_TOKEN", "ath_secret")
    monkeypatch.setenv("ATHENA_WAKE_WORD", "1")
    monkeypatch.setenv("ATHENA_WAKE_WORD_MODEL", str(model))

    module = types.ModuleType("openwakeword.model")

    def broken_model(**kwargs: object) -> None:
        raise RuntimeError("ONNX runtime: modèle illisible")

    module.Model = broken_model
    monkeypatch.setitem(sys.modules, "openwakeword.model", module)

    with pytest.raises(SystemExit) as excinfo:
        cli()

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "Veille indisponible" in err
    assert "athena.onnx" in err
    assert "Traceback" not in err


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
