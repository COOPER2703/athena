from __future__ import annotations

import ast
from pathlib import Path

TRANSPORT_DIR = Path(__file__).resolve().parent.parent / "adapters" / "transport"

FORBIDDEN_MODULES = {
    "logging",
    "server.core.coordinator",
    "server.core.state",
}

FORBIDDEN_NAMES = {
    "Coordinator",
    "SessionState",
    "set_active_client",
    "send_to_active",
    "active_client_id",
    "_active_client_id",
}


def _imports(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            modules.add(node.module.split(".")[0])
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")
    return modules


def _names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def test_transport_package_contains_modules() -> None:
    assert sorted(TRANSPORT_DIR.glob("*.py"))


def test_guard_catches_forbidden_submodule_import_forms() -> None:
    tree = ast.parse(
        "import logging.handlers\n"
        "from server.core import coordinator\n"
        "from server.core.state import SessionState\n"
    )
    assert _imports(tree) & FORBIDDEN_MODULES == {
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
    assert not (_imports(tree) & FORBIDDEN_MODULES)


def test_transport_imports_no_session_rules_or_logging() -> None:
    for path in sorted(TRANSPORT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        found = _imports(tree) & FORBIDDEN_MODULES
        assert not found, f"{path.name} imports forbidden modules: {sorted(found)}"


def test_transport_has_no_active_client_or_session_state() -> None:
    for path in sorted(TRANSPORT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        found = _names(tree) & FORBIDDEN_NAMES
        assert not found, f"{path.name} references forbidden names: {sorted(found)}"
