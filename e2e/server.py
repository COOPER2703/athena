"""Serveur de test des parcours navigateur (ticket #179).

Ce script est le **superviseur** des parcours Playwright : il démarre le vrai
serveur du harnais partagé (``server.tests.server_harness.running_server``) sur
un port fixe, puis le relance à la même adresse après chaque sortie propre.
C'est exactement ce qu'un déploiement réel attend d'``ATHENA_RESTART_ON_APPLY`` :
le serveur sort, le superviseur le relance.

Le modèle Live est injecté par ``E2ELiveModel`` : il reçoit le texte du navigateur,
appelle le Tool du noyau ``save_memory``, puis rend un texte et un court audio.
Un message qui contient « lentement » fait attendre la réponse finale, ce qui
laisse le temps d'observer un Tour en cours et de l'interrompre. Il répond aussi
à l'audio du micro simulé du parcours vocal : mots entendus, puis texte et
audio déterministes.

Un second serveur HTTP, sur le port de contrôle, permet au test de demander un
redémarrage propre (parcours 1 : après la clé Gemini, un redémarrage applique la
configuration), de constater les générations successives, et de faire tenir une
Session à un Client desktop pour prouver le refus de la Console sans préemption.
Il n'existe **que** dans cet harnais : l'application ne connaît ni ce port ni ces
routes.

Rien n'est lu dans l'environnement du processus pour la configuration : le
harnais injecte un environnement vide, donc aucun Secret réel n'est utilisé, et
``FakeSecretValidator`` accepte la clé factice sans aucun appel réseau.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import struct
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from aiohttp import web

REPO_ROOT = Path(__file__).resolve().parent.parent
# ``server_harness`` importe le harnais ``harness`` du même dossier : le seam
# partagé vit dans ``server/tests`` et n'est pas un paquet installable.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "server" / "tests"))

from server_harness import (  # noqa: E402
    FakeLiveModel,
    FakeSecretValidator,
    running_server,
)

# --- valeurs partagées avec les parcours Playwright -------------------------
# Toute modification doit être répercutée dans ``web/e2e/constants.ts``.

USERNAME = "e2e"
PASSWORD = "athena-e2e-password"
#: Clé factice acceptée par ``FakeSecretValidator`` : aucun appel réseau.
GEMINI_KEY = "e2e-fake-gemini-key"
#: Réponse finale du modèle, texte et audio, pour un Tour ordinaire.
REPLY_TEXT = "Bonjour, je suis Athena. Réponse de test de bout en bout."
#: Réponse d'un Tour lent : elle ne doit jamais arriver avant l'interruption.
SLOW_REPLY_TEXT = "Réponse lente : ce texte ne doit pas être affiché."
#: Marqueur lu dans le message pour ralentir le Tour.
SLOW_MARKER = "lentement"
#: Délai avant la réponse finale ordinaire : le Tool et l'activité restent lisibles.
NORMAL_DELAY = 1.2
#: Délai du Tour lent : le test interrompt bien avant.
SLOW_DELAY = 30.0
#: Audio descendant : 0,4 s de PCM 16 bits mono 24 kHz (contrat du flux).
AUDIO_PCM = b"".join(
    struct.pack("<h", int(8_000 * math.sin(2 * math.pi * 440 * index / 24_000)))
    for index in range(9_600)
)
#: Paroles que le faux modèle attribue au micro simulé du parcours vocal.
#: Chromium rejoue ``client/tests/data/athena.wav`` ; le faux modèle ne l'écoute
#: pas et rend cette transcription d'entrée déterministe.
VOICE_HEARD_TEXT = "Bonjour Athena, ceci est un test vocal."
#: Réponse texte et audio à l'audio du micro simulé.
VOICE_REPLY_TEXT = "Je vous entends bien. Réponse vocale de test."


class E2ELiveModel(FakeLiveModel):
    """Faux modèle qui scripte seul chaque Tour, sans pilotage Python.

    Un Tour reçu produit d'abord un appel de Tool du noyau (``save_memory``),
    puis — quand le serveur rend le résultat du Tool — un texte et un audio.
    Après une interruption, l'état repart proprement au Tour suivant.

    L'audio du **micro simulé** suit un autre script : la première trame reçue
    rend les mots entendus (immédiatement), puis la réponse texte et audio
    après le délai ordinaire. Le parcours vocal rejoue le même fichier toutes
    les ~64 ms ; une seule réponse par Connexion Live suffit et évite une
    rafale de tours que le vrai modèle ne produirait pas.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending_final = False
        self._slow = False
        #: Nombre de trames audio déjà examinées. Sa croissance distingue un
        #: déclencheur audio d'un résultat de Tool, qui n'ajoute aucune trame.
        self._audio_seen = 0
        #: Connexions Live qui ont déjà reçu leur réponse vocale. Une nouvelle
        #: conversation a le droit à la sienne ; une même conversation non.
        self._audio_answered: set[int] = set()

    async def _trigger(self, session: Any) -> None:
        if len(self.received_audio) > self._audio_seen:
            self._audio_seen = len(self.received_audio)
            if id(session) not in self._audio_answered:
                self._audio_answered.add(id(session))
                # Deux tours, comme un vrai modèle qui transcrit avant de
                # parler : les mots de la personne sont visibles tout de suite,
                # la réponse arrive ensuite.
                self.reply(heard=VOICE_HEARD_TEXT)
                self.reply(
                    text=VOICE_REPLY_TEXT,
                    audio=AUDIO_PCM,
                    delay=NORMAL_DELAY,
                )
                # Chaque entrée livre le tour suivant ; deux entrées pour deux
                # tours, sans attendre la trame suivante du micro.
                await super()._trigger(session)
                await super()._trigger(session)
            return
        if self._turns.empty():
            if self._pending_final:
                self._pending_final = False
                self.reply(
                    text=SLOW_REPLY_TEXT if self._slow else REPLY_TEXT,
                    audio=AUDIO_PCM,
                    delay=SLOW_DELAY if self._slow else NORMAL_DELAY,
                )
            else:
                heard = self.received_texts[-1] if self.received_texts else ""
                self._slow = SLOW_MARKER in heard.lower()
                self._pending_final = True
                self.call_tool(
                    "save_memory",
                    {
                        "content": "La Console web a été vérifiée de bout en bout.",
                        "kind": "fact",
                    },
                )
        await super()._trigger(session)


class ControlServer:
    """Petit serveur de contrôle : redémarrage, générations et Session occupée.

    Le port de contrôle n'est jamais exposé par l'application : il est propre
    au harnais, et le processus Playwright est le seul client. Le test y lit la
    génération courante (preuve qu'un redémarrage a bien eu lieu) et peut
    demander une sortie propre, que le superviseur transforme en relance. Il y
    fait aussi tenir une Session par un Client desktop, pour que la voix de la
    Console rencontre un noyau déjà occupé.
    """

    def __init__(self, port: int, console_port: int) -> None:
        self._port = port
        self._console_port = console_port
        self._runner: web.AppRunner | None = None
        self._server: Any = None
        self._generation = 0
        self._ready = False
        #: Pile de sortie de la connexion du Client desktop qui occupe la Session.
        self._occupant: AsyncExitStack | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def attach(self, server: Any, generation: int) -> None:
        self._server = server
        self._generation = generation
        self._ready = True

    def detach(self) -> None:
        self._server = None
        self._ready = False

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/state", self._handle_state)
        app.router.add_post("/restart", self._handle_restart)
        app.router.add_post("/session/occupy", self._handle_occupy)
        app.router.add_post("/session/release", self._handle_release)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()

    async def stop(self) -> None:
        await self.release_occupant()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_state(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "generation": self._generation,
                "ready": self._ready,
                "url": f"http://127.0.0.1:{self._console_port}",
            }
        )

    async def _handle_restart(self, request: web.Request) -> web.Response:
        server = self._server
        if server is None:
            return web.json_response({"restarting": False}, status=409)
        server.app.request_shutdown()
        # ``requested`` est déjà vrai : le superviseur relance à la sortie.
        return web.json_response({"restarting": True}, status=202)

    async def _handle_occupy(self, request: web.Request) -> web.Response:
        """Fait tenir une Session à un Client desktop du Protocole partagé.

        La voix de la Console doit alors refuser (``session_busy``) au lieu de
        préempter. Le Client reste ouvert jusqu'à ``/session/release``.
        """
        server = self._server
        if server is None:
            return web.json_response({"occupied": False}, status=409)
        if self._occupant is not None:
            return web.json_response({"occupied": True})
        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(
                server.client(name="desktop", platform="linux")
            )
            await client.start_session()
        except BaseException:
            await stack.aclose()
            raise
        self._occupant = stack
        return web.json_response(
            {"occupied": True, "client_id": client.client_id}
        )

    async def _handle_release(self, request: web.Request) -> web.Response:
        """Libère la Session occupée et attend le retour au Repos."""
        server = self._server
        await self.release_occupant()
        if server is not None:
            try:
                await server.wait_until(
                    lambda: server.app.snapshot()["session"]["state"] == "Idle",
                    timeout=2.0,
                )
            except (AssertionError, asyncio.TimeoutError):
                return web.json_response({"occupied": True}, status=409)
        return web.json_response({"occupied": False})

    async def release_occupant(self) -> None:
        """Referme la connexion du Client desktop, si elle existe encore."""
        stack = self._occupant
        self._occupant = None
        if stack is None:
            return
        try:
            await stack.aclose()
        except Exception:
            # Un serveur déjà arrêté a pu fermer le socket : rien à libérer.
            pass


async def ensure_account(server: Any) -> None:
    """Crée l'unique Compte sur une instance déjà installée, une seule fois.

    Le parcours 2 part d'une instance installée : le test se connecte comme le
    ferait un propriétaire. Le parcours 1 crée lui-même le Compte ; ce semis ne
    s'applique donc qu'au mode ``installed``.
    """
    async with server.anonymous() as probe:
        async with probe.get(f"{server.url}/api/session") as response:
            identity = await response.json()
    if identity.get("setup_required") is True:
        await server.install(username=USERNAME, password=PASSWORD)


async def wait_for_shutdown(
    server: Any, stop: asyncio.Event
) -> None:
    """Attend la sortie propre du serveur, ou une demande d'arrêt du superviseur."""
    waiter = asyncio.create_task(server.wait_stopped(None))
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, stopper}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        stopper.cancel()
    if waiter in done:
        # Une exception du serveur remonte ici et tue le superviseur : un
        # démarrage raté doit être visible, jamais relancé en boucle.
        waiter.result()
        return
    if not waiter.done():
        server.app.request_shutdown()
        await waiter
    else:
        waiter.result()


async def supervise(options: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for handled in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(handled, stop.set)

    static_dir = options.static_dir.resolve()
    if not (static_dir / "index.html").is_file():
        raise SystemExit(
            f"Build de la SPA absent ({static_dir}). Lancez « make web » avant les E2E."
        )

    control = ControlServer(options.control_port, options.port)
    await control.start()
    generation = 0
    gemini_key = GEMINI_KEY if options.state == "installed" else ""
    try:
        while not stop.is_set():
            model = E2ELiveModel()
            async with running_server(
                options.root,
                static_dir=static_dir,
                gemini_key=gemini_key,
                model=model,
                # La clé factice est acceptée sans aucun appel réseau.
                secret_validator=FakeSecretValidator(),
                console_port=options.port,
                transport_port=options.transport_port,
                restart_on_apply=True,
            ) as server:
                if options.state == "installed":
                    await ensure_account(server)
                control.attach(server, generation)
                print(
                    json.dumps(
                        {
                            "url": server.url,
                            "control": control.url,
                            "generation": generation,
                        }
                    ),
                    flush=True,
                )
                await wait_for_shutdown(server, stop)
                await control.release_occupant()
                control.detach()
            generation += 1
    finally:
        await control.stop()
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--transport-port", type=int, required=True)
    parser.add_argument("--control-port", type=int, required=True)
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=REPO_ROOT / "web" / "dist",
    )
    parser.add_argument(
        "--state",
        choices=("fresh", "installed"),
        default="fresh",
        help="fresh : aucun Compte ni clé ; installed : Compte semé, clé factice.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(sys.argv[1:] if argv is None else argv)
    options.root.mkdir(parents=True, exist_ok=True)
    return asyncio.run(supervise(options))


if __name__ == "__main__":
    raise SystemExit(main())
