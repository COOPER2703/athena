from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from pydantic import ValidationError

from server.adapters.plugins import PluginLoadError
from server.app.config import default_env_file, load_config
from server.app.root import App, build_app
from server.app.secrets import MissingSecretError


def _install_signal_handlers(app: App) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.request_shutdown)
        except NotImplementedError:
            pass


async def main(
    *, env: dict[str, str] | None = None, env_file: Path | None = None
) -> None:
    """Charge la configuration, compose l'application et exécute une boucle unique."""
    resolved_env_file = env_file if env_file is not None else default_env_file()
    config = load_config(env=env, env_file=resolved_env_file)
    # Les secrets de plugin sont résolus par build_app depuis la même vue
    # fusionnée (``.env`` recouvert par l'environnement) que la configuration.
    app = build_app(config, env=env, env_file=resolved_env_file)
    _install_signal_handlers(app)
    await app.run()


def cli() -> None:
    try:
        asyncio.run(main())
    except (MissingSecretError, ValidationError, PluginLoadError) as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    cli()
