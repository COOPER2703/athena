# Smoke test — T5 Console web (`python -m server` + navigateur + `npm run dev`)

Vérification manuelle de bout en bout de la **Console web** (spec #62, ADR-0002, ADR-0003 et
ADR-0016) : une SPA servie par le serveur, alimentée par une API JSON versionnée et un
WebSocket de traces, qui montre l'état de la Session, des Clients, des Plugins et de la
configuration, permet d'injecter un Événement et de filtrer le flux — sans terminal. La
Console **n'est pas un Client** : elle ne parle pas le Protocole partagé, ne s'enregistre pas
et ne reçoit pas d'audio.

Voir `docs/smoke-test-t1-composition.md` pour la boucle serveur/client d'origine,
`docs/smoke-test-t2-memoire.md` pour la mémoire, `docs/smoke-test-t3-evenements.md` pour le
Canal d'événements et le Webhook, et `docs/smoke-test-t4-plugins.md` pour les Plugins.

## Prérequis

Toutes les commandes se lancent **depuis la racine du dépôt** (le python de la venv locale
s'écrit donc `.venv/bin/python`).

- Python et les dépendances de développement : `uv sync --extra dev`.
- Le protocole généré : `make generate` (ou `python protocol/proto/generate.py`).
- Node 24+ et npm (validé ici avec Node 26 / npm 11) ; les dépendances du front :
  `npm ci` dans `web/`, ou directement `make web`.
- Un `GEMINI_API_KEY`. Pour ce smoke test, une clé **factice** suffit à démarrer et à
  vérifier toute la Console ; un Événement injecté sera alors jugé par le modèle texte, qui
  échouera en faute fermée (trace `EventJudgmentFailed`) sans bloquer la surface. Le parcours
  vocal complet reste hors périmètre de ce document.
- Un navigateur (Chrome/Chromium/Firefox) pour les étapes d'interface.

### Configuration de la Console

La Console est activée par défaut sur `127.0.0.1:8767`, sans authentification en v1. Les
variables d'environnement (ou le `.env` à la racine) suivent le même schéma que le Webhook :

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `ATHENA_CONSOLE_ENABLED` | `1` | `0` désactive complètement la surface |
| `ATHENA_CONSOLE_HOST` | `127.0.0.1` | loopback par défaut |
| `ATHENA_CONSOLE_PORT` | `8767` | port HTTP et WebSocket de la Console |
| `ATHENA_CONSOLE_STATIC_DIR` | `web/dist` | build de la SPA ; absent, page explicative |
| `ATHENA_CONSOLE_TRACE_BUFFER` | `500` | Traces rejouées à la connexion |

Le Webhook reste indépendant (`ATHENA_WEBHOOK_ENABLED`, `ATHENA_WEBHOOK_HOST`,
`ATHENA_WEBHOOK_PORT`, `127.0.0.1:8766` par défaut). Le build du front n'est **jamais
committé** : `web/dist/` est un produit de `make web`.

## 1. Préparer et démarrer

```sh
# Dépendances et protocole.
uv sync --extra dev
make generate

# Front : installation, vérification, tests et build.
make web-check
make web-test
make web

# Lancement du serveur. La Console n'a pas de trace de démarrage dédiée :
# c'est GET /api/status qui confirme qu'elle écoute.
python -m server
```

Attendu :

- `make web` produit `web/dist/index.html`, un bundle JS et une feuille CSS.
- Le serveur trace `[app] started {"state": "Idle"}` sur `stderr` (le flux est aussi écrit
  dans `logs/server.log`).
- `GET http://127.0.0.1:8767/api/status` répond `200`. La Console écoute sur le port
  configuré ; le Webhook reste sur le sien.

Pour isoler un run, on peut déplacer les effets de bord :

```sh
ATHENA_MEMORY_DB=/tmp/athena-smoke.db \
ATHENA_LOG_DIR=/tmp/athena-smoke-logs \
ATHENA_CONSOLE_PORT=8767 \
python -m server
```

## 2. API JSON

### 2.1 `GET /api/status`

```sh
curl -s http://127.0.0.1:8767/api/status | python -m json.tool
```

L'enveloppe est **versionnée** (`v: 1`) et en `snake_case` ; les clés de `config` sont une
liste blanche, sans secret :

```json
{
  "v": 1,
  "session": {
    "state": "Idle",
    "session_id": "",
    "connection_id": "",
    "active_client_id": ""
  },
  "clients": [],
  "plugins": [
    { "name": "homeassistant", "state": "disabled", "tools": [] }
  ],
  "config": {
    "model": "gemini-3.1-flash-live-preview",
    "voice": "Leda",
    "user_id": "owner",
    "memory_db": "/tmp/athena-smoke.db",
    "log_dir": "/tmp/athena-smoke-logs",
    "version": "0.1.0"
  }
}
```

La forme exacte compte : `session` porte `state`, `session_id`, `connection_id` et
`active_client_id` ; `clients` est une liste de `{client_id, client_name, platform}` ;
`plugins` une liste de `{name, state, tools}` ; `config` les six champs ci-dessus. Un Client
enregistré apparaît dans `clients` (nom et plateforme) ; une Session en cours désigne son
Client actif dans `session.active_client_id`.

### 2.2 Chemins inconnus sous `/api`

```sh
curl -i http://127.0.0.1:8767/api/unknown
curl -i http://127.0.0.1:8767/api
curl -i http://127.0.0.1:8767/api/stream   # sans en-tête Upgrade
```

Attendu :

- `/api/unknown` et `/api` → `404`, `Content-Type: application/json`,
  `{"error": "not found"}` (jamais une page HTML) ;
- `/api/stream` sans `Upgrade: websocket` → `400` JSON
  `{"error": "websocket required"}`.

Tout chemin **hors** `/api` (`/`, `/chemin/inconnu`) appartient à la SPA et reçoit son entrée
(page unique, sans routeur), ou la page explicative si le build est absent (étape 6).

### 2.3 `POST /api/events`

```sh
# Source par défaut : console.
curl -i -X POST http://127.0.0.1:8767/api/events \
  -H 'Content-Type: application/json' \
  -d '{"content": "La porte d entree vient de s ouvrir"}'

# Source explicite.
curl -i -X POST http://127.0.0.1:8767/api/events \
  -H 'Content-Type: application/json' \
  -d '{"source": "maison", "content": "Le garage est ouvert"}'
```

Attendu : `202 Accepted` et `{"status": "accepted"}`. L'Événement entre dans le **Canal
d'événements unique** par la couture `push` et une Trace `[console] event_received` apparaît
dans le flux, avec `source` et `chars` dans le payload. La validation est **la même** que le
Webhook (module partagé `server/adapters/event_body.py`), seule la Source par défaut change.

Rejets attendus de la validation partagée, tous en JSON :

| Requête | Code | Message |
| --- | --- | --- |
| `Content-Type: text/plain` | `415` | `Content-Type must be application/json` |
| corps JSON invalide | `400` | `invalid JSON body` |
| contenu vide après trim | `400` | `source and content must be non-empty` |
| contenu > 4 096 caractères | `413` | `content exceeds 4096 characters` |
| surface sans couture `push` | `503` | `console not serving` |

Un corps qui dépasse la garde mémoire de 64 Kio (65 536 octets) n'atteint jamais cette
validation : aiohttp le refuse avant, en `413 text/plain` avec le message
`Maximum request body size 65536 exceeded.` Ce rejet **n'est pas du JSON** et ne porte donc
pas la forme `{"error": …}` des autres.

Exemple pour les trois cas vérifiables sur un serveur sain :

```sh
curl -i -X POST http://127.0.0.1:8767/api/events \
  -H 'Content-Type: text/plain' -d 'hello'
curl -i -X POST http://127.0.0.1:8767/api/events \
  -H 'Content-Type: application/json' -d '{not json'
curl -i -X POST http://127.0.0.1:8767/api/events \
  -H 'Content-Type: application/json' -d '{"content":"   "}'
curl -i -X POST http://127.0.0.1:8767/api/events \
  -H 'Content-Type: application/json' \
  -d "{\"content\": \"$(printf 'x%.0s' {1..4097})\"}"
```

Le `503` n'est **pas** atteignable depuis `python -m server` en marche normale : la couture
`push` est toujours fournie par la racine de composition. Il correspond à une Console montée
sans injection (adaptateur testé isolément) et est couvert par
`server/tests/test_console_events.py::test_without_a_push_seam_the_injection_is_not_serving`.

## 3. WebSocket `/api/stream`

Le flux est **unidirectionnel** : le serveur envoie un unique `hello`, puis des trames
`trace` vivantes, et des trames `status` quand — et seulement quand — l'instantané change.
La Console n'écrit rien sur le socket.

Pour observer le flux brut :

```sh
.venv/bin/python - <<'PY'
import asyncio, json
import aiohttp

async def main() -> None:
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect("http://127.0.0.1:8767/api/stream")
        for _ in range(3):
            print(json.dumps(await ws.receive_json(), ensure_ascii=False))
        await ws.close()

asyncio.run(main())
PY
```

Dans un autre terminal, injectez un Événement (étape 2.3) entre deux lectures : une trame
`trace` doit apparaître.

### 3.1 `hello` : instantané + backlog

Attendu, en première trame :

```json
{
  "v": 1,
  "type": "hello",
  "data": {
    "snapshot": { "session": { "...": "..." }, "clients": [], "plugins": [], "config": {} },
    "backlog": [
      { "timestamp": 1793038246.445, "source": "plugins", "kind": "PluginState", "level": "info", "payload": {} }
    ]
  }
}
```

`snapshot` est l'instantané de **lecture** pris au moment de la connexion (même forme que
`/api/status`, sans l'enveloppe). `backlog` contient les dernières Traces du tampon borné
(`ATHENA_CONSOLE_TRACE_BUFFER`, 500 par défaut), **tous niveaux confondus**. La lecture du
backlog et l'abonnement sont atomiques : ni trou ni doublon entre l'historique et le direct.

### 3.2 Trames `trace` vivantes

Après l'injection, attendu :

```json
{
  "v": 1,
  "type": "trace",
  "data": {
    "timestamp": 1793038300.795,
    "source": "console",
    "kind": "event_received",
    "level": "info",
    "payload": { "source": "console", "chars": 35 }
  }
}
```

Les Traces du Webhook suivent le même chemin et sont visibles dans la Console :
`{"source":"webhook","kind":"event_received",...,"payload":{"source":"maison","chars":20}}`.
Un client trop lent reçoit une **unique** trame de troncature
(`kind: "TraceTruncated"`, `payload.dropped`) ; le navigateur l'affiche comme un compteur
(« Flux tronqué : N entrée(s) perdue(s). ») sans l'insérer dans le log. Ce cas est couvert
par `server/tests/test_console_stream.py`.

### 3.3 Trame `status` seulement sur changement

Une trame `status` porte une enveloppe `v`/`type` et sa clé `data` est exactement le corps de
`/api/status` (sans `v`, qui est dans l'enveloppe). Elle est émise au premier tick (environ
1 s) qui constate un changement structurel de l'instantané : un Client qui s'enregistre, une
Session qui commence, un Plugin qui change d'état. Sans changement, **aucune** trame n'est
poussée.

Vérification : connectez le flux, puis enregistrez un client audio sur le port WebSocket du
serveur (Protocole partagé, voir étape 7). Attendu : une trame `status` dont `data.clients`
contient `{client_id, client_name, platform}`. Laissez ensuite le flux tranquille : aucune
autre trame `status` ne doit arriver.

### 3.4 Reconnexion et redémarrage du serveur

1. Connectez le flux et notez le `hello` (instantané et taille du backlog).
2. Arrêtez le serveur (`Ctrl-C` ou `SIGTERM`) : le WebSocket se ferme proprement et le
   serveur trace `[app] stopped`.
3. Relancez `python -m server` et reconnectez le flux.

Attendu : un **nouveau** `hello`, avec l'état courant et le backlog du **nouveau** processus
(pas de concaténation avec l'ancien). Le front fait cette reconnexion seul, avec un backoff
exponentiel (500 ms → 30 s, plafonné) ; chaque `hello` **remplace** l'état et le log affichés.

## 4. Navigateur

Ouvrez `http://127.0.0.1:8767/`. Attendu :

1. **Thème sombre et dense**, en-tête « Athena · Console web · surface d'observation »,
   bouton « Actualiser ».
2. **Session** : état traduit (« Inactive » au repos), identifiant de Session (`—` s'il n'y
   en a pas), connexion, Client actif (« Aucun » sans Session).
3. **Clients** : liste des Clients branchés (nom, plateforme, identifiant court) ; badge
   « Client actif » sur le Client propriétaire de la Session. « Aucun Client branché. » sinon.
4. **Plugins** : nom, état traduit (« Activé », « Désactivé », « Manquant », « En échec ») et
   Tools exposés (ou « Aucun Tool »).
5. **Configuration** : modèle Live, voix, utilisateur, chemins mémoire et journaux, version.
   Aucun secret.
6. **Traces** : le backlog s'affiche, puis les Traces défilent. Chaque ligne montre l'heure
   **locale** (`HH:MM:SS.mmm`), le niveau, la source entre crochets, le kind, et un payload
   JSON repliable.
7. **Filtres** : niveau (options présentes dans le flux), source, texte libre (recherche
   insensible à la casse et aux accents sur source, kind et payload). « Réinitialiser »
   remet les trois filtres à zéro.
8. **Pause / Reprendre** : la pause gèle la vue, pas les données ; un bandeau indique
   « Affichage gelé · N nouvelle(s) trace(s) ». « Reprendre » rattrape le flux.
9. **Défilement auto** : le bouton bascule entre « Défilement auto » et « Défilement
   suspendu » ; en pause, l'autoscroll ne force pas la vue.
10. **Injection** : formulaire « Injection d'un Événement », Source préremplie `console`,
    compteur `0 / 4096 caractères`, refus local si vide ou trop long, message
    « Événement accepté » et nouvelle Trace `event_received` en cas de succès.
11. **Flux tronqué** : bandeau « Flux tronqué : N entrée(s) perdue(s). » si le marqueur
    serveur a été reçu.
12. **Reconnexion** : l'en-tête du panneau Traces affiche « Connexion… », « Connecté »,
    « Reconnexion… » ou « Déconnecté ». Après un redémarrage, la page se resynchronise sans
    rechargement.

## 5. Mode développement Vite

Le proxy du serveur de développement évite de rebuilder à chaque modification. Dans un
terminal, la Console doit tourner :

```sh
python -m server
```

Dans un second terminal :

```sh
cd web
ATHENA_CONSOLE_PORT=8767 npm run dev
```

`ATHENA_CONSOLE_PORT` doit être le port **réel** de la Console (le défaut est 8767). Vite
sert la page sur `http://localhost:5173/` (port par défaut) et proxifie `/api` — REST **et**
WebSocket — vers `http://127.0.0.1:8767`. Attendu :

- la page se charge depuis Vite, avec rechargement à chaud ;
- `GET /api/status` via le proxy répond `200` avec le même JSON ;
- `ws://localhost:5173/api/stream` reçoit le `hello`, puis les Traces vivantes ;
- `POST /api/events` via le proxy répond `202`.

Sans le proxy, la page Vite ne trouverait ni `/api/status` ni `/api/stream` : c'est le seul
point de configuration du mode dev.

## 6. Absence de build

Le build n'est pas requis pour que l'API réponde. Arrêtez le serveur, puis relancez-le avec
un répertoire statique vide :

```sh
ATHENA_CONSOLE_STATIC_DIR=/tmp/athena-no-build python -m server
```

Attendu :

- `GET /` → `200 text/html`, page « Console Athena — build absent » qui indique
  `make web` (et `npm ci && npm run build`) ;
- tout chemin hors `/api` renvoie la même page explicative, pas une 404 muette ;
- `GET /api/status` → `200`, `POST /api/events` → `202` : l'API reste pleinement
  fonctionnelle.

Avec le build (`make web` puis lancement par défaut) :

- `GET /` → l'`index.html` de la SPA, qui référence `/assets/index-*.js` et `.css` ;
- `GET /assets/index-*.js` → `200`, `Content-Type: text/javascript` ;
- `GET /chemin/inconnu` → le même `index.html` (repli SPA) ;
- l'API répond toujours.

## 7. Non-régression

### Client audio (Protocole partagé)

Le Protocole partagé n'est pas modifié par la Console. Pour le prouver sans micro, enregistrez
un client factice sur le WebSocket du serveur (même extrait qu'au smoke test T4, étape 12) :

```sh
.venv/bin/python - <<'PY'
import asyncio, websockets
from protocol import Register, decode, encode

async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        await ws.send(encode(Register(client_name="smoke-console", platform="linux")))
        registered = decode(await ws.recv())
        print("client_id:", registered.client_id)
        print("server_tools:", registered.server_tools)

asyncio.run(main())
PY
```

Attendu : `Registered` avec un `client_id` et `server_tools` (`save_memory`, `forget_memory`,
plus les Tools de plugin le cas échéant). Le Client apparaît dans le panneau Clients de la
Console et dans `GET /api/status` — sans devenir un Client de la Console : c'est un vrai
Client audio, vu par la surface d'observation. Le parcours audio réel (`python -m client`,
micro/haut-parleur) n'est pas automatisable ici ; il reste couvert par les tests existants.

### Webhook `POST /events`

Le contrat du Webhook est inchangé :

```sh
curl -i -X POST http://127.0.0.1:8766/events \
  -H 'Content-Type: application/json' \
  -d '{"source": "maison", "content": "Le garage est ouvert"}'
```

Attendu : `202 Accepted` + `{"status": "accepted"}`, puis une Trace
`[webhook] event_received {"chars": 20, "source": "maison"}` visible aussi dans la Console.
Les codes de rejet du Webhook (`415`, `400`, `413`, `404`, `405`) restent ceux du smoke test
T3.

### `logs/server.log`

Le fichier de traces existe et reçoit les mêmes lignes que `stderr` :

```sh
ls -l logs/
tail -n 5 logs/server.log
```

Attendu : `logs/server.log` (rotation par taille en `server.log.1` … `server.log.3` quand le
seuil est atteint), avec les lignes `[app] started`, `[console] event_received`,
`[webhook] event_received`, `[app] stopped`. La Console ne crée **aucun** fichier de traces
propre : elle consomme le flux unique (ADR-0004). Un secret (`GEMINI_API_KEY` factice inclus)
n'apparaît ni dans l'instantané, ni dans les logs.

## Vérification automatisée

```sh
make generate
PYTHONPATH=$PWD .venv/bin/python -m pytest server/tests protocol/tests client/tests -q

cd web
npm ci
npm run typecheck
npm test
npm run build
```

Les comportements de la Console sont couverts par :

- `server/tests/test_console_adapter.py` — `/api/status`, 404 JSON, page explicative, build
  et repli SPA, chemin statique borné ;
- `server/tests/test_console_events.py` — injection, Source par défaut `console`, 415/400/413,
  `503` sans couture, entrée réelle dans le Canal ;
- `server/tests/test_console_stream.py` — `hello` + backlog, direct, atomicité, troncature,
  plusieurs clients, arrêt propre, câblage depuis le fan-out ;
- `server/tests/test_console_status.py` — une trame par changement, pas de trame sans
  changement, état courant à la reconnexion ;
- `server/tests/test_app_config.py` — `ConsoleConfig` et variables d'environnement ;
- `server/tests/test_app_integration.py` — câblage réel : un Client enregistré apparaît dans
  l'instantané, une Session désigne son Client actif, un Événement console entre dans le
  Canal ;
- `web/src/*.test.ts` — filtres, pause, formulaire, réduction des trames, backoff de
  reconnexion, horodatage local, panneaux (55 tests, sans navigateur).

## Résultat du run

**Date** : 2026-10-26 · **Machine** : poste de dev Linux (CET) · **Python** : 3.14.8 (venv
locale) · **aiohttp** : 3.14.5 · **Node** : 26.8.2 / npm 11.19.1 · **Chrome** : 155.0.8059.84
(headless).

**Ce qui a été exécuté pour de vrai** :

Les chiffres cités ci-dessous sont les mesures de ce run daté (taille du bundle, nombre de
lignes de log, compteurs de traces, ports) : ils varient avec la machine, les versions et
l'état du serveur. Ce sont des résultats observés, pas des invariants du contrat.

1. **Génération et suites** — `make generate`, puis
   `pytest server/tests protocol/tests client/tests -q` : `653 passed, 1 warning in 25.94s`.
   Front : `npm ci` (46 paquets), `npm run typecheck` (aucune erreur), `npm test`
   (`6 fichiers, 55 tests`), `npm run build` (`28 modules`, bundle JS 236,00 kB, CSS 6,83 kB).
   Les cibles `make web-check`, `make web-test` et `make web` ont aussi été exécutées avec
   succès ; le `Makefile` mentionnait déjà les trois cibles front, aucune correction n'a été
   nécessaire.

2. **Serveur réel sans build** — `WS_PORT=39311`, Webhook `35643`, Console `33581`,
   `ATHENA_CONSOLE_STATIC_DIR` vide, base mémoire et journaux dans `/tmp`. Démarrage
   `[app] started {"state": "Idle"}` ; `GET /` renvoie la page « build absent » (1101 octets)
   et `/some/spa/path` la même page, tandis que `/api/status` répond `200` et
   `POST /api/events` `202`.

3. **API réelle** — `/api/status` renvoie l'enveloppe exacte ci-dessus (`v: 1`, Session
   `Idle`, `clients: []`, plugin `homeassistant` `disabled`, `config` sans secret) ;
   `/api/unknown` et `/api` renvoient `404 {"error": "not found"}` ; `/api/stream` sans
   `Upgrade` renvoie `400 {"error": "websocket required"}` ; l'injection sans Source est
   tracée `[console] event_received {"chars": 35, "source": "console"}`, l'injection avec
   `source: "maison"` conserve cette Source dans le payload. Les rejets réels observés :
   `415`, `400` (JSON invalide), `400` (contenu vide), `413` (> 4096 caractères). Le `503`
   n'a pas été provoqué sur un serveur sain (voir étape 2.3) et reste couvert par le test
   dédié.

4. **WebSocket réel** — `hello` reçu avec `snapshot` et `backlog` ; après enregistrement
   d'un Client audio factice (`Registered(client_id='…', server_tools=['save_memory',
   'forget_memory'])`), une trame `status` a poussé `clients: [{client_name: "smoke-t5",
   platform: "linux"}]` dans les ~1 s du tick ; après une injection `POST /api/events`, une
   trame `trace` `[console] event_received` est arrivée ; un `POST` sur le Webhook a produit
   `[webhook] event_received {"chars": 20, "source": "maison"}`. Sur 2,5 s sans changement,
   **aucune** trame `status` supplémentaire. Redémarrage réel : la socket s'est fermée,
   `[app] stopped`, puis après relance un nouveau `hello` a été reçu
   (`backlog` 88 → 6, `state: Idle`) : l'état et le log ont bien été remplacés, pas
   concaténés.

5. **Navigateur réel (Chrome headless + CDP)** — page chargée depuis le build servi par la
   Console, connexion « Connecté », panneaux Session/Clients/Plugins/Configuration rendus
   (Plugin `homeassistant` « Désactivé », « Aucun Tool »), 39 Traces dans le backlog,
   formulaire avec Source `console` et compteur `0 / 4096 caractères`, horodatage local
   `19:12:06.881` (les logs serveur correspondants sont en UTC `18:12:06`, soit CET).
   Interactions jouées : filtre texte `event_received` → 1 ligne ; filtre niveau `error` →
   2 lignes toutes `ERROR` ; filtre sans résultat → « Aucune trace ne correspond aux
   filtres. » ; pause → vue gelée à 39 lignes, bandeau « Affichage gelé · 4 nouvelle(s)
   trace(s) » pendant qu'un Événement injecté par le formulaire était accepté ; reprise →
   43 lignes ; bascule autoscroll → « Défilement suspendu » ; un Client `smoke-browser` est
   apparu en direct dans le panneau Clients puis a disparu à sa déconnexion. Le rendu a été
   observé par les lectures DOM via CDP (texte des panneaux, lignes de traces, bandeaux) ;
   aucune capture n'est committée, et aucune vérification visuelle humaine écran par écran
   n'a été faite au-delà de ces lectures.

6. **Mode dev Vite réel** — serveur Console sur `33581`, `npm run dev -- --port 35173` dans
   `web/` avec `ATHENA_CONSOLE_PORT=33581`. Vite 8.3.5 démarre ; `GET /api/status` via le
   proxy renvoie `200` et `v: 1` ; le WebSocket `ws://localhost:35173/api/stream` reçoit un
   `hello` ; `POST /api/events` via le proxy renvoie `202` et la trame `trace`
   `event_received` arrive. Dans cet environnement, Vite écoute `localhost` en `::1` (pas
   `127.0.0.1`) : utiliser `http://localhost:35173`, ou `--host 127.0.0.1`.

7. **Build réel** — après `make web`, `GET /` sert l'`index.html` (404 octets),
   `/assets/index-QosxAiUn.js` répond `200 text/javascript` (236 004 octets) et
   `/chemin/inconnu` renvoie exactement le même `index.html`.

8. **Non-régression réelle** — client audio factice enregistré via le Protocole partagé
   (`Registered`, `server_tools` du noyau) et visible dans la Console ; Webhook `202` et
   Trace `[webhook]` ; `logs/server.log` présent (134 lignes), contenant `[app] started`,
   `[console] event_received`, `[webhook] event_received` et `[app] stopped` ; la clé Gemini
   factice n'apparaît ni dans l'instantané ni dans les logs. Arrêt propre (`[app] stopped`)
   du serveur et de Vite.

**Différé à un humain (non automatisable ici) et pourquoi** :

- **Parcours vocal réel** : micro, haut-parleur et `GEMINI_API_KEY` valide ne sont pas
  disponibles ; le smoke test vérifie le Protocole partagé par un enregistrement factice, pas
  une conversation.
- **Jugement d'un Événement** : avec la clé factice, le juge échoue en faute fermée
  (`EventJudgmentFailed`) ; l'injection prouve l'entrée dans le Canal, pas le verdict.
- **Troncature d'un client lent** : le marqueur est couvert par
  `test_console_stream.py::test_slow_client_loses_the_oldest_and_gets_a_single_truncation_marker`
  et affiché par le front, mais n'a pas été forcé sur le run réel.
- **`503` de l'injection** : non atteignable sur un serveur sain ; couvert par le test dédié.
- **Vérification visuelle humaine** : la capture headless prouve le rendu et les
  interactions DOM, pas la lisibilité à l'œil sur un écran donné.
