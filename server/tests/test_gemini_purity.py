from __future__ import annotations

import ast
from pathlib import Path

GEMINI_DIR = Path(__file__).resolve().parent.parent / "adapters" / "gemini"

FORBIDDEN_MODULES = {
    "logging",
    "server.core.coordinator",
    "server.core.state",
}

ENVIRONMENT_NAMES = {"environ", "getenv"}


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


def test_gemini_package_contains_modules() -> None:
    assert sorted(GEMINI_DIR.glob("*.py"))


def test_gemini_imports_no_session_rules_or_logging() -> None:
    for path in sorted(GEMINI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        found = _imports(tree) & FORBIDDEN_MODULES
        assert not found, f"{path.name} imports forbidden modules: {sorted(found)}"


def test_gemini_reads_no_environment_variables() -> None:
    for path in sorted(GEMINI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        assert "os" not in _imports(tree), f"{path.name} imports os"
        found = _names(tree) & ENVIRONMENT_NAMES
        assert not found, f"{path.name} references env names: {sorted(found)}"
