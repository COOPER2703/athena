from __future__ import annotations

import ast
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "core"

FORBIDDEN = {
    "asyncio",
    "socket",
    "ssl",
    "http",
    "aiohttp",
    "websockets",
    "sqlite3",
    "threading",
    "multiprocessing",
    "subprocess",
    "logging",
}


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module.split(".")[0])
    return modules


def test_core_imports_no_io_or_asyncio():
    files = sorted(CORE_DIR.glob("*.py"))
    assert files, "expected the server.core package to contain modules"

    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        found = _imported_modules(tree) & FORBIDDEN
        assert not found, f"{path.name} imports forbidden modules: {sorted(found)}"
