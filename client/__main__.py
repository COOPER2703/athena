from __future__ import annotations

import asyncio
import logging
import signal
import sys

from client.config import AppConfig
from client.daemon import ClientDaemon
from client.settings import SettingsStore

log = logging.getLogger("athena.client")


async def _enter_loop(daemon: ClientDaemon) -> None:
    """Démarre une Session (ou barge-in) à chaque Entrée sur stdin."""
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if line == "":
            return
        try:
            await daemon.tap()
        except Exception:
            log.exception("Échec du démarrage de Session ignoré")


async def main(shutdown: asyncio.Event | None = None) -> None:
    """Démarre la page de couplage puis le Client audio.

    Aucun Jeton n'est exigé ici : sans couplage, la page s'affiche et invite à
    en saisir un. C'est elle qui répare une URL ou un Jeton invalide, sans
    redémarrer le processus.
    """
    cfg = AppConfig()
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO))

    daemon = ClientDaemon(cfg, SettingsStore(cfg.page.config_path))
    await daemon.start()

    loop = asyncio.get_running_loop()
    stop = shutdown if shutdown is not None else asyncio.Event()

    def request_stop() -> None:
        log.info("Arrêt...")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    enter_task = asyncio.create_task(_enter_loop(daemon))
    shutdown_task = asyncio.create_task(stop.wait())

    await asyncio.wait([shutdown_task], return_when=asyncio.FIRST_COMPLETED)

    for task in (enter_task, shutdown_task):
        task.cancel()
    await asyncio.gather(enter_task, shutdown_task, return_exceptions=True)
    await daemon.stop()


def cli() -> None:
    try:
        asyncio.run(main())
    except OSError as exc:
        # Le cas courant est un port déjà pris : mieux vaut le dire que laisser
        # une trace d'exception brute.
        print(f"Page de couplage indisponible : {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    cli()
