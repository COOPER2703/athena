from __future__ import annotations

import asyncio
import logging
import signal
import sys

from client.app import ClientApp
from client.audio_io import AudioIO
from client.config import AppConfig
from client.ws_client import WsTransport

log = logging.getLogger("athena.client")


def _build_app(cfg: AppConfig) -> ClientApp:
    transport = WsTransport(cfg.client)
    return ClientApp(cfg, audio=AudioIO(cfg.audio), transport=transport)


async def _enter_loop(app: ClientApp) -> None:
    """Démarre une Session (ou barge-in) à chaque Entrée sur stdin."""
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if line == "":
            return
        try:
            await app.tap()
        except Exception:
            log.exception("Échec du démarrage de Session ignoré")


async def main() -> None:
    cfg = AppConfig()
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO))

    app = _build_app(cfg)

    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()

    def stop() -> None:
        log.info("Arrêt...")
        shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except NotImplementedError:
            pass

    run_task = asyncio.create_task(app.run())
    enter_task = asyncio.create_task(_enter_loop(app))
    shutdown_task = asyncio.create_task(shutdown.wait())

    await asyncio.wait(
        [run_task, shutdown_task], return_when=asyncio.FIRST_COMPLETED
    )

    for task in (run_task, enter_task, shutdown_task):
        task.cancel()
    await asyncio.gather(
        run_task, enter_task, shutdown_task, return_exceptions=True
    )


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
