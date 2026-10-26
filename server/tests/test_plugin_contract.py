from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "plugins"
PLUGIN_API_DIR = REPO_ROOT / "plugin_api"

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
    """Noms de modules importés, pointés (``server.core.plugins``, pas ``server``)."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def test_no_plugin_imports_a_server_internal() -> None:
    """Le contrat public suffit : aucun plugin ne touche aux internes du serveur."""
    files = sorted(PLUGINS_DIR.rglob("*.py"))
    assert files, "expected the plugins directory to contain modules"

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders = sorted(
            module
            for module in _imported_modules(tree)
            if module == "server" or module.startswith("server.")
        )
        assert not offenders, (
            f"{path.relative_to(REPO_ROOT)} imports server internals: {offenders}"
        )


def test_plugin_api_is_importable_without_the_server() -> None:
    """``import plugin_api`` ne charge aucun module de ``server``."""
    code = (
        "import sys\n"
        "import plugin_api\n"
        "leaked = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name == 'server' or name.startswith('server.')\n"
        ")\n"
        "assert not leaked, leaked\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_plugin_api_imports_no_io_or_asyncio() -> None:
    """Le contrat reste pur : types et erreurs, aucune I/O."""
    files = sorted(PLUGIN_API_DIR.glob("*.py"))
    assert files, "expected the plugin_api package to contain modules"

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found = {module.split(".")[0] for module in _imported_modules(tree)}
        assert not found & FORBIDDEN, (
            f"{path.name} imports forbidden modules: {sorted(found & FORBIDDEN)}"
        )


def test_kernel_modules_reexport_the_public_contract() -> None:
    """Les imports existants du noyau continuent de marcher (ré-exports)."""
    import plugin_api
    from server.core import plugins, ports, tools

    assert plugins.PluginBinding is plugin_api.PluginBinding
    assert plugins.PluginContext is plugin_api.PluginContext
    assert plugins.ToolBinding is plugin_api.ToolBinding
    assert plugins.PluginManifest is plugin_api.PluginManifest
    assert plugins.ConfigField is plugin_api.ConfigField
    assert plugins.SecretField is plugin_api.SecretField
    assert plugins.SERVER_PLATFORM == plugin_api.SERVER_PLATFORM
    assert ports.ToolSpec is plugin_api.ToolSpec
    assert tools.ToolHandler is plugin_api.ToolHandler
