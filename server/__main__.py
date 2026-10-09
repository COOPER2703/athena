from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from pydantic import ValidationError

from server.app.config import load_config
from server.app.root import App, build_app
from server.app.secrets import MissingSecretError


def _default_env_file() -> Path:
    return Path.cwd() / ".env"


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
    config = load_config(
        env=env,
        env_file=env_file if env_file is not None else _default_env_file(),
    )
    app = build_app(config)
    _install_signal_handlers(app)
    await app.run()


def cli() -> None:
    try:
        asyncio.run(main())
    except (MissingSecretError, ValidationError) as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    cli()
