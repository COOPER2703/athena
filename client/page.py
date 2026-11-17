"""Page locale de couplage du Client desktop.

Le daemon sert une page minimale en boucle locale pour saisir l'**URL du
serveur** et le **Jeton d'accès** sans éditer de fichier. Elle montre aussi
l'état du micro, du Wake word et de la connexion, et permet de relancer une
connexion.

Deux garde-fous, indépendants de la page elle-même :

- toute requête dont l'en-tête ``Host`` n'est pas une adresse de boucle locale,
  ou dont l'en-tête ``Origin`` ne correspond pas à cet hôte, reçoit ``403`` ;
- les réponses d'état ne rendent jamais de secret : seuls les champs publics
  d'un état connu sont sérialisés, quel que soit ce que le contrôleur fournit.

La page est autonome (HTML, CSS et JavaScript inline) : elle s'affiche même
quand le serveur Athena est injoignable, puisqu'elle est servie par le Client.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any, Awaitable, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from aiohttp import web

log = logging.getLogger("athena.client")

# Champs publics de l'état : liste blanche, pour qu'un Jeton ajouté par erreur
# au dictionnaire du contrôleur ne parte jamais sur le réseau.
PUBLIC_STATE_KEYS = frozenset(
    {
        "server_url",
        "server_url_source",
        "token_configured",
        "token_source",
        "connection",
        "connection_message",
        "mic",
        "mic_message",
        "wake_word",
        "wake_word_message",
        "client_state",
        "client_id",
    }
)

_PAGE_CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


class CouplingController(Protocol):
    """Contrat consommé par la page ; le daemon l'implémente."""

    def coupling_state(self) -> Mapping[str, object]: ...

    async def apply_coupling(
        self, *, server_url: str, access_token: str | None
    ) -> Mapping[str, object]: ...

    async def reconnect(self) -> Mapping[str, object]: ...


def is_websocket_url(value: str) -> bool:
    """Vrai pour une URL ``ws://`` ou ``wss://`` avec un hôte."""
    parts = urlsplit(value)
    return parts.scheme in {"ws", "wss"} and bool(parts.hostname)


def _hostname(host_header: str) -> str | None:
    """Hôte d'un en-tête ``Host`` (port et crochets IPv6 retirés)."""
    try:
        return urlsplit(f"//{host_header}").hostname
    except ValueError:
        return None


def is_local_host(host_header: str) -> bool:
    """``localhost`` ou une adresse de boucle locale, avec ou sans port."""
    hostname = _hostname(host_header)
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _origin_matches(origin: str, host_header: str) -> bool:
    """Même origine que la page : schéma web et hôte identique à ``Host``."""
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False
    return parts.netloc.lower() == host_header.lower()


def _public_state(state: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in state.items() if key in PUBLIC_STATE_KEYS}


def _no_store(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    merged = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if headers:
        merged.update(headers)
    return merged


class CouplingPage:
    """Serveur HTTP local de la page de couplage.

    ``port=0`` lie un port éphémère (tests) ; ``port`` expose le port réel.
    """

    def __init__(
        self,
        controller: CouplingController,
        *,
        host: str = "127.0.0.1",
        port: int = 8768,
    ) -> None:
        self._controller = controller
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        """Port réellement lié (utile quand ``port=0``)."""
        server = self._site._server if self._site is not None else None
        sockets = getattr(server, "sockets", None)
        if not sockets:
            raise RuntimeError("Coupling page not started")
        return sockets[0].getsockname()[1]

    async def start(self) -> None:
        app = web.Application(middlewares=[self._local_only])
        app.router.add_get("/", self._handle_page)
        app.router.add_get("/api/state", self._handle_state)
        app.router.add_post("/api/coupling", self._handle_coupling)
        app.router.add_post("/api/reconnect", self._handle_reconnect)
        app.router.add_route("*", "/{tail:.*}", self._handle_not_found)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    @web.middleware
    async def _local_only(
        self,
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        """Refuse tout hôte ou toute origine qui n'est pas de boucle locale."""
        host = request.headers.get("Host", "")
        if not is_local_host(host):
            return self._forbidden(request, "Hôte non local.")
        origin = request.headers.get("Origin")
        if origin is not None and not _origin_matches(origin, host):
            return self._forbidden(request, "Origine non locale.")
        return await handler(request)

    def _forbidden(self, request: web.Request, message: str) -> web.Response:
        if request.path.startswith("/api/"):
            return web.json_response(
                {"error": "forbidden", "message": message},
                status=403,
                headers=_no_store(),
            )
        return web.Response(
            text=message,
            status=403,
            content_type="text/plain",
            headers=_no_store(),
        )

    async def _handle_page(self, request: web.Request) -> web.Response:
        return web.Response(
            text=_PAGE_HTML,
            content_type="text/html",
            headers=_no_store(
                {
                    "Content-Security-Policy": _PAGE_CSP,
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                }
            ),
        )

    async def _handle_state(self, request: web.Request) -> web.Response:
        return web.json_response(
            _public_state(self._controller.coupling_state()), headers=_no_store()
        )

    async def _handle_coupling(self, request: web.Request) -> web.Response:
        payload = await self._payload(request)
        if payload is None:
            return self._invalid_body()
        raw_url = payload.get("server_url")
        server_url = raw_url.strip() if isinstance(raw_url, str) else ""
        if not is_websocket_url(server_url):
            return web.json_response(
                {
                    "error": "invalid_url",
                    "message": (
                        "L'URL du serveur doit commencer par ws:// ou wss:// "
                        "et nommer un hôte."
                    ),
                },
                status=400,
                headers=_no_store(),
            )
        raw_token = payload.get("access_token")
        if raw_token is None:
            access_token = None
        elif isinstance(raw_token, str):
            access_token = raw_token.strip() or None
        else:
            return self._invalid_body()
        try:
            state = await self._controller.apply_coupling(
                server_url=server_url, access_token=access_token
            )
        except OSError:
            return web.json_response(
                {
                    "error": "store_error",
                    "message": "Impossible d'enregistrer le couplage local.",
                },
                status=500,
                headers=_no_store(),
            )
        return web.json_response(_public_state(state), headers=_no_store())

    async def _handle_reconnect(self, request: web.Request) -> web.Response:
        state = await self._controller.reconnect()
        return web.json_response(_public_state(state), headers=_no_store())

    async def _payload(self, request: web.Request) -> Mapping[str, Any] | None:
        """Corps JSON ou formulaire ; ``None`` si le corps est inexploitable."""
        if request.content_type == "application/json":
            try:
                payload = await request.json()
            except ValueError:
                return None
        else:
            try:
                payload = await request.post()
            except ValueError:
                return None
        if not isinstance(payload, Mapping):
            return None
        return payload

    def _invalid_body(self) -> web.Response:
        return web.json_response(
            {
                "error": "invalid_body",
                "message": "Corps de requête illisible.",
            },
            status=400,
            headers=_no_store(),
        )

    async def _handle_not_found(self, request: web.Request) -> web.Response:
        if request.path.startswith("/api/"):
            return web.json_response(
                {"error": "not_found", "message": "Route inconnue."},
                status=404,
                headers=_no_store(),
            )
        return web.Response(
            text="Page inconnue.",
            status=404,
            content_type="text/plain",
            headers=_no_store(),
        )


_PAGE_HTML = """<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="referrer" content="no-referrer" />
    <title>Athena — couplage du client</title>
    <style>
      :root {
        color-scheme: dark;
        --fond: #0d1013;
        --panneau: #14191e;
        --bord: #263039;
        --texte: #dfe6ec;
        --doux: #9aa7b2;
        --accent: #a7e8bd;
        --alerte: #f0c674;
        --erreur: #ef9a9a;
        --rayon: 10px;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        padding: 2.5rem 1.25rem 4rem;
        background: var(--fond);
        color: var(--texte);
        font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
        line-height: 1.55;
      }
      main { width: min(46rem, 100%); margin: 0 auto; }
      .eyebrow {
        margin: 0 0 0.35rem;
        color: var(--accent);
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.75rem;
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }
      h1 { margin: 0 0 0.6rem; font-size: clamp(1.5rem, 4vw, 2rem); }
      h2 {
        margin: 0 0 0.9rem;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.82rem;
        font-weight: 600;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: var(--doux);
      }
      .lede { margin: 0 0 2rem; color: var(--doux); max-width: 62ch; }
      section {
        margin-bottom: 1.25rem;
        padding: 1.4rem;
        border: 1px solid var(--bord);
        border-radius: var(--rayon);
        background: var(--panneau);
      }
      dl { margin: 0; display: grid; gap: 0.85rem; }
      dl > div {
        display: flex;
        flex-wrap: wrap;
        gap: 0.35rem 0.75rem;
        align-items: baseline;
        justify-content: space-between;
      }
      dt { color: var(--doux); font-size: 0.92rem; }
      dd { margin: 0; text-align: right; }
      .valeur {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.92rem;
      }
      .valeur[data-tone="ok"] { color: var(--accent); }
      .valeur[data-tone="warn"] { color: var(--alerte); }
      .valeur[data-tone="error"] { color: var(--erreur); }
      .valeur[data-tone="idle"] { color: var(--doux); }
      .detail { display: block; margin-top: 0.2rem; color: var(--doux); font-size: 0.82rem; text-align: right; }
      label { display: block; margin: 1rem 0 0.4rem; font-size: 0.92rem; }
      input {
        width: 100%;
        padding: 0.65rem 0.75rem;
        border: 1px solid var(--bord);
        border-radius: 8px;
        background: #0b0e11;
        color: var(--texte);
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.95rem;
      }
      input:disabled { color: var(--doux); background: #11161a; }
      input:focus-visible,
      button:focus-visible {
        outline: 2px solid var(--accent);
        outline-offset: 2px;
      }
      .note { margin: 0.45rem 0 0; color: var(--doux); font-size: 0.82rem; }
      .actions { display: flex; flex-wrap: wrap; gap: 0.7rem; margin-top: 1.3rem; }
      button {
        padding: 0.6rem 1rem;
        border: 1px solid var(--bord);
        border-radius: 8px;
        background: transparent;
        color: var(--texte);
        font: inherit;
        cursor: pointer;
      }
      button:hover { border-color: var(--doux); }
      button.primary {
        border-color: var(--accent);
        background: var(--accent);
        color: #0d1013;
        font-weight: 600;
      }
      button.primary:hover { filter: brightness(1.08); }
      .alert {
        margin: 1rem 0 0;
        padding: 0.7rem 0.85rem;
        border: 1px solid var(--bord);
        border-left: 3px solid var(--alerte);
        border-radius: 6px;
        color: var(--texte);
        background: #1a1d1f;
      }
      .alert[data-tone="error"] { border-left-color: var(--erreur); }
      .alert[data-tone="ok"] { border-left-color: var(--accent); }
      @media (prefers-reduced-motion: no-preference) {
        button { transition: border-color 120ms ease, filter 120ms ease; }
      }
    </style>
  </head>
  <body>
    <main>
      <header>
        <p class="eyebrow">Client Athena</p>
        <h1>Couplage de ce poste</h1>
        <p class="lede">
          Saisissez l'adresse du serveur et un Jeton d'accès de nature
          « client ». Le Jeton est enregistré dans un fichier privé et n'est
          plus jamais affiché.
        </p>
      </header>

      <section aria-labelledby="etat-titre">
        <h2 id="etat-titre">État</h2>
        <dl>
          <div>
            <dt>Serveur</dt>
            <dd>
              <span id="connection" class="valeur" data-tone="idle">—</span>
              <span id="connection-message" class="detail"></span>
            </dd>
          </div>
          <div>
            <dt>Micro</dt>
            <dd>
              <span id="mic" class="valeur" data-tone="idle">—</span>
              <span id="mic-message" class="detail"></span>
            </dd>
          </div>
          <div>
            <dt>Wake word</dt>
            <dd>
              <span id="wake_word" class="valeur" data-tone="idle">—</span>
              <span id="wake_word-message" class="detail"></span>
            </dd>
          </div>
          <div>
            <dt>Client</dt>
            <dd><span id="client_state" class="valeur" data-tone="idle">—</span></dd>
          </div>
        </dl>
        <p id="alert" class="alert" role="status" aria-live="polite" hidden></p>
      </section>

      <section aria-labelledby="couplage-titre">
        <h2 id="couplage-titre">Couplage</h2>
        <form id="coupling" novalidate>
          <label for="server-url">URL du serveur</label>
          <input
            id="server-url"
            name="server_url"
            type="url"
            inputmode="url"
            autocomplete="off"
            spellcheck="false"
            required
            placeholder="ws://localhost:8765"
          />
          <p id="url-note" class="note" hidden></p>

          <label for="access-token">Jeton d'accès</label>
          <input
            id="access-token"
            name="access_token"
            type="password"
            autocomplete="off"
            spellcheck="false"
            placeholder="ath_…"
          />
          <p id="token-note" class="note"></p>

          <div class="actions">
            <button type="submit" class="primary">Enregistrer</button>
            <button type="button" id="reconnect">Reconnecter</button>
          </div>
        </form>
      </section>
    </main>

    <script>
      (() => {
        const $ = (id) => document.getElementById(id);
        const form = $("coupling");
        const urlInput = $("server-url");
        const tokenInput = $("access-token");
        const alertBox = $("alert");
        let urlDirty = false;

        const LABELS = {
          connection: {
            idle: "En attente",
            connecting: "Connexion…",
            connected: "Connecté",
            disconnected: "Déconnecté",
            error: "Injoignable",
            refused: "Jeton refusé",
            unconfigured: "À coupler",
          },
          mic: { unknown: "Inconnu", ready: "Actif", unavailable: "Indisponible" },
          wake_word: {
            disabled: "Désactivé",
            loading: "Chargement…",
            ready: "Actif",
            unavailable: "Indisponible",
          },
          client_state: {
            listening: "En écoute",
            active: "En session",
            stopped: "Arrêté",
          },
        };
        const TONES = {
          connection: {
            connected: "ok",
            connecting: "warn",
            unconfigured: "warn",
            refused: "error",
            error: "error",
            disconnected: "warn",
            idle: "idle",
          },
          mic: { ready: "ok", unavailable: "error", unknown: "idle" },
          wake_word: {
            ready: "ok",
            disabled: "idle",
            loading: "warn",
            unavailable: "error",
          },
          client_state: { active: "ok", listening: "ok", stopped: "idle" },
        };

        function status(id, value, message) {
          const node = $(id);
          node.textContent = LABELS[id][value] || value || "—";
          node.dataset.tone = TONES[id][value] || "idle";
          const detail = $(id + "-message");
          if (detail) detail.textContent = message || "";
        }

        function announce(text, tone) {
          alertBox.textContent = text;
          alertBox.dataset.tone = tone || "warn";
          alertBox.hidden = !text;
        }

        function render(state) {
          if (!urlDirty) urlInput.value = state.server_url || "";
          const urlFromEnv = state.server_url_source === "environment";
          urlInput.disabled = urlFromEnv;
          const urlNote = $("url-note");
          urlNote.hidden = !urlFromEnv;
          urlNote.textContent = urlFromEnv
            ? "URL imposée par WS_SERVER_URL dans l'environnement du processus."
            : "";

          const tokenFromEnv = state.token_source === "environment";
          tokenInput.disabled = tokenFromEnv;
          if (tokenFromEnv) {
            tokenInput.placeholder = "Jeton imposé par ATHENA_ACCESS_TOKEN";
          } else if (state.token_configured) {
            tokenInput.placeholder = "•••••••• (configuré) — laisser vide pour conserver";
          } else {
            tokenInput.placeholder = "ath_…";
          }
          $("token-note").textContent = tokenFromEnv
            ? "Le Jeton est imposé par ATHENA_ACCESS_TOKEN ; cette page ne peut pas le remplacer."
            : state.token_configured
              ? "Un Jeton est déjà enregistré : laissez le champ vide pour le conserver."
              : "Aucun Jeton enregistré : copiez-en un depuis la Console, Jetons d'accès.";

          status("connection", state.connection, state.connection_message);
          status("mic", state.mic, state.mic_message);
          status("wake_word", state.wake_word, state.wake_word_message);
          status("client_state", state.client_state, "");
        }

        async function readState() {
          const response = await fetch("/api/state", {
            headers: { Accept: "application/json" },
            cache: "no-store",
          });
          if (!response.ok) throw new Error("état indisponible");
          return response.json();
        }

        async function refresh() {
          try {
            render(await readState());
          } catch (error) {
            announce("La page locale ne répond pas.", "error");
          }
        }

        async function send(path, body, success) {
          try {
            const response = await fetch(path, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
              cache: "no-store",
            });
            const payload = await response.json();
            if (!response.ok) {
              announce(payload.message || "Requête refusée.", "error");
              return;
            }
            render(payload);
            announce(success, "ok");
          } catch (error) {
            announce("Le Client ne répond pas.", "error");
          }
        }

        urlInput.addEventListener("input", () => {
          urlDirty = true;
        });

        form.addEventListener("submit", (event) => {
          event.preventDefault();
          const serverUrl = urlInput.value.trim();
          const token = tokenInput.value;
          tokenInput.value = "";
          send(
            "/api/coupling",
            { server_url: serverUrl, access_token: token },
            "Couplage enregistré."
          );
        });

        $("reconnect").addEventListener("click", () => {
          send("/api/reconnect", {}, "Reconnexion demandée.");
        });

        refresh();
        setInterval(refresh, 2000);
      })();
    </script>
  </body>
</html>
"""
