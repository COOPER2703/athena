from __future__ import annotations

import ast
from pathlib import Path

import ast_guard

GEMINI_DIR = Path(__file__).resolve().parent.parent / "adapters" / "gemini"

ENVIRONMENT_NAMES = {"environ", "getenv"}


def test_gemini_package_contains_modules() -> None:
    assert sorted(GEMINI_DIR.glob("*.py"))


def test_guard_catches_forbidden_submodule_import_forms() -> None:
    tree = ast.parse(
        "import logging.handlers\n"
        "from server.core import coordinator\n"
        "from server.core.state import SessionState\n"
    )
    assert ast_guard.imports(tree) & ast_guard.FORBIDDEN_MODULES == {
        "logging",
        "server.core.coordinator",
        "server.core.state",
    }


def test_guard_allows_legitimate_core_seams() -> None:
    tree = ast.parse(
        "from server.core.commands import Trace\n"
        "from server.core.events import Event\n"
        "from server.core.ports import TraceSink\n"
    )
    assert not (ast_guard.imports(tree) & ast_guard.FORBIDDEN_MODULES)


def test_gemini_imports_no_session_rules_or_logging() -> None:
    for path in sorted(GEMINI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        found = ast_guard.imports(tree) & ast_guard.FORBIDDEN_MODULES
        assert not found, f"{path.name} imports forbidden modules: {sorted(found)}"


def test_gemini_has_no_active_client_or_session_state() -> None:
    for path in sorted(GEMINI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        found = ast_guard.names(tree) & ast_guard.FORBIDDEN_NAMES
        assert not found, f"{path.name} references forbidden names: {sorted(found)}"


def test_gemini_reads_no_environment_variables() -> None:
    for path in sorted(GEMINI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        assert "os" not in ast_guard.imports(tree), f"{path.name} imports os"
        found = ast_guard.names(tree) & ENVIRONMENT_NAMES
        assert not found, f"{path.name} references env names: {sorted(found)}"
