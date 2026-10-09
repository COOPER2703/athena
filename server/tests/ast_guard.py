from __future__ import annotations

import ast

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


def imports(tree: ast.AST) -> set[str]:
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


def names(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found
