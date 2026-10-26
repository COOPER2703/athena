from __future__ import annotations

import ast
from pathlib import Path

ADAPTERS_DIR = Path(__file__).resolve().parent.parent / "adapters"


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_adapters_import_nothing_from_the_app_layer() -> None:
    """ADR-0001 : un adaptateur dépend du noyau, jamais de la racine de composition."""
    files = sorted(ADAPTERS_DIR.rglob("*.py"))
    assert files, "expected the adapters package to contain modules"

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders = sorted(
            module
            for module in _imported_modules(tree)
            if module == "server.app" or module.startswith("server.app.")
        )
        assert not offenders, (
            f"{path.relative_to(ADAPTERS_DIR.parent.parent)} imports the app "
            f"layer: {offenders}"
        )


def test_secret_port_lives_in_the_kernel_and_the_app_reexports_it() -> None:
    from server.app import secrets as app_secrets
    from server.core import ports

    assert app_secrets.SecretResolver is ports.SecretResolver
    assert app_secrets.MissingSecretError is ports.MissingSecretError
    assert app_secrets.EnvSecretResolver
