## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### Server logs

The server mirrors its trace stream to `logs/server.log` (same rendered lines as
`stderr`, filtered by `LOG_LEVEL`). The file rotates by size into
`server.log.1`…`server.log.3`. Override the directory with `ATHENA_LOG_DIR`.
Set `ATHENA_DEBUG_DECISIONS=1` to include `debug` entries. Read this file to
diagnose a past run without access to the server terminal.

### Console web

La Console web est une surface d'observation (ADR-0003), distincte du Webhook. Elle
écoute par défaut sur `127.0.0.1:8767`, **activée**. Depuis la spec #75, elle exige un
**Ticket Console** (cookie `HttpOnly; Secure; SameSite=Strict`) pour toute route d'API
hors identité : seule la SPA, `/healthz`, `/api/session`, `/api/setup` et `/api/login`
restent publiques. Sa configuration suit le même schéma que le Webhook :

- `ATHENA_CONSOLE_ENABLED` : `1` par défaut ; `0` désactive complètement la surface.
- `ATHENA_CONSOLE_HOST` : `127.0.0.1` par défaut (loopback ; ne l'ouvrir au réseau qu'en
  connaissance de cause).
- `ATHENA_CONSOLE_PORT` : `8767` par défaut.
- `ATHENA_CONSOLE_STATIC_DIR` : `web/dist` par défaut ; répertoire du build de la SPA
  (produit par `make web`). Absent, la Console sert une page explicative et l'API répond
  normalement.
- `ATHENA_CONSOLE_TRACE_BUFFER` : `500` par défaut ; nombre de Traces récentes rejouées à la
  connexion de `GET /api/stream` (WebSocket : une trame `hello`, puis des trames `trace`).

Le **Diagnostic** lit `GET /api/status` et le WebSocket `/api/stream`. L'instantané porte en
plus de `session`, `clients`, `plugins` et `config` : `installation` (`account_exists`,
`gemini_configured`, `complete`, `secrets_unreadable`, `surfaces` réellement ouvertes),
`restart_required`, et `health` (version, uptime monotone, dernière Consolidation, taille des
bases mémoire/auth/réglages/planifications — `0` si absente, `-wal`/`-shm` compris —, dernière
erreur sans son payload, taille et capacité du Tampon de traces). Ces champs sont des lectures
**sans secret** et restent renseignés pendant l'Installation : aucun n'exige un Compte ni une clé.
Pendant une conversation vocale, `clients` contient en plus le **Client Console** (nom
`Console (<navigateur>)`, plateforme `web`) et `session.console` vaut `true` : le front sait
que la Session en cours est celle qu'il héberge.

La **conversation vocale** (ADR-0027) passe par `GET /api/voice`, une WebSocket authentifiée
par le **Ticket Console** comme `/api/stream` : trames binaires montantes PCM 16 bits mono
16 kHz, trames JSON descendantes `ready` (la Connexion Live est ouverte), `transcript`
(`role`, `text`), `audio` (base64, `audio/pcm;rate=24000`), `ended` (`reason` du protocole) et
`refused` (`code` stable, `message` lisible). La page ajoute `?tab=<identifiant>` : la
reconnexion automatique du même onglet remplace son propre Client Console abandonné (coupure
pas encore constatée), un second onglet reste refusé. Le serveur y héberge un **Client Console**
éphémère : il naît au démarrage, tient une Session ordinaire, meurt à sa fin
(`CLIENT_GONE`, `COMPLETED`, `TIMEOUT`), et se refuse à préempter la Session d'un autre
Client (`session_busy`) ou un Tour (`tour_busy`).

Le Webhook reste séparé (`ATHENA_WEBHOOK_ENABLED`, `ATHENA_WEBHOOK_HOST`,
`ATHENA_WEBHOOK_PORT`, `127.0.0.1:8766` par défaut). Le contrat JSON de la Console
commence par `GET /api/status` ; le front vit dans `web/` et n'est pas requis pour que
l'API réponde.

### Authentification

Le module d'auth vit dans `server/adapters/auth/` (port `AuthService` dans
`server/core/ports.py`). Il possède sa propre base SQLite, **distincte de la mémoire** :
`ATHENA_AUTH_DB` (`auth.db` par défaut, relatif au répertoire de travail). Les tables
(Comptes, Tickets Console, Jetons d'accès) sont créées par `CREATE TABLE IF NOT EXISTS`,
sans migration. L'Installation ne demande **aucun code** (ADR-0026) : au premier
démarrage sans Compte, `POST /api/setup` crée l'unique Compte et le premier visiteur
devient propriétaire ; une fois le Compte créé, la route refuse définitivement
(`409 already_installed`). Aucune trace `InstallationCode` n'existe plus.

- `ATHENA_CONSOLE_TICKET_DAYS` : `30` jours glissants par défaut.
- `ATHENA_CONSOLE_TICKET_REFRESH_SECONDS` : `3600` (l'expiration n'est repoussée qu'au
  plus une fois par heure).
- `ATHENA_AUTH_PASSWORD_MIN_LENGTH` : `12` caractères minimum, imposés côté serveur
  (hachage Argon2id).
- `ATHENA_AUTH_LOGIN_MAX_ATTEMPTS` : `5` échecs avant blocage.
- `ATHENA_AUTH_LOGIN_BLOCK_MINUTES` : `15` minutes de blocage, levé automatiquement.

Les deux mécanismes de credential (ADR-0017) :

- **Ticket Console** : cookie `athena_ticket` (`HttpOnly; Secure; SameSite=Strict`, `Path=/`),
  30 jours glissants. Il authentifie toutes les routes d'API hors identité et les WebSockets
  du flux et de la conversation vocale ; `GET /healthz` reste public.
- **Jeton d'accès** : `Authorization: Bearer ath_…`, nature `client` (transport) ou `source`
  (Webhook). Le transport (`WS_HOST` / `WS_PORT`) refuse le handshake sans Jeton `client` par
  `401` + `WWW-Authenticate: Bearer`, avant toute trame. Le Webhook
  (`ATHENA_WEBHOOK_ENABLED` / `ATHENA_WEBHOOK_HOST` / `ATHENA_WEBHOOK_PORT`) exige un Jeton
  `source` ; sa Source fait autorité et une Source de corps contradictoire est refusée `403`.
- **Client desktop** : le Jeton se saisit sur la **page locale de couplage** du daemon, ou
  reste fourni par `ATHENA_ACCESS_TOKEN` (surcharge de processus prioritaire). Il part au
  handshake en `Authorization: Bearer` et n'est jamais réaffiché.

### Réglages, Secrets et Installation

Le **magasin de réglages** vit dans `server/adapters/settings/` (ports `SettingsStore` /
`SecretStore` dans `server/core/ports.py`) : base SQLite dédiée
`ATHENA_SETTINGS_DB` (`settings.db` par défaut) et **clé maître** chiffrante
`ATHENA_MASTER_KEY_FILE` (`master.key` par défaut), fichier `0600` **séparé** du magasin.
Les Secrets sont chiffrés au repos en AES-GCM (`cryptography`) : la clé maître est générée
au `POST /api/setup` et renvoyée **une seule fois** dans la réponse (`master_key`, à
sauvegarder avec le magasin). Perdue, le serveur démarre quand même, expose
`secrets_unreadable` dans `GET /api/session` et demande de re-saisir les Secrets ; rien
n'est réinitialisé en silence.

- `GET /api/session` expose `setup_required` (aucun Compte), `authenticated`, `username`,
  `gemini_configured`, `installation_complete`, `restart_required`, `secrets_unreadable`,
  et `password_min_length` : la longueur minimale du mot de passe est expliquée à l'écran
  d'installation avec la valeur que le serveur applique, jamais une constante du front.
- `GET /api/settings` rend chaque réglage avec sa valeur **effective** (celle que le
  processus a chargée), son défaut, sa source (`default` | `store` | `env`), ses bornes et
  ses valeurs autorisées, `pending` / `pending_value`, la liste `pending`, un objet
  `restart`, la liste `surfaces` (`name`, `host`, `port`, `scheme`, `url` active) et les
  Secrets par nom (`configured`, `updated_at`, jamais de valeur).
  `PUT /api/settings` reçoit `{values: {champ: texte}, confirm_url?}`, exige l'objet
  **complet** des champs éditables non masqués, valide tout avant la première écriture et
  rend les erreurs par champ dans `fields` (`error: invalid_settings`, `400`).
- Un champ masqué par l'environnement est en lecture seule et un `PUT` qui le change est
  refusé ; renvoyer sa valeur courante est accepté sans rien écrire. Les chemins des bases,
  des Plugins et des journaux sont refusés par nom, jamais seulement par valeur.
- Un changement d'`hôte` ou de `port` (Console, Webhook, transport) exige `confirm_url`
  égal à l'URL calculée (`http://hôte:port` pour la Console et le Webhook, `ws://hôte:port`
  pour le transport ; une liste si plusieurs surfaces changent).
- **Changements en attente** = écart entre le magasin et la configuration chargée au
  démarrage ; une valeur égale au défaut efface l'entrée du magasin. L'application passe par
  un redémarrage : `ATHENA_RESTART_ON_APPLY=1` fait sortir proprement le processus (le
  superviseur le relance), sinon `GET/PUT` exposent `restart.required` et la commande
  exacte `ATHENA_RESTART_COMMAND` (`docker restart athena` par défaut).
- `GET /api/secrets` : noms, `configured`, `updated_at`, jamais de valeur.
  `PUT /api/secrets/{nom}` valide la clé Gemini par un appel réel
  (`count_tokens`) avant de l'enregistrer ; `DELETE /api/secrets/{nom}` l'efface.
- `POST /api/chat` ouvre un **Tour** texte (ADR-0024) et répond en
  `text/event-stream` : événements `text`, `audio` (base64, PCM 16 bits mono 24 kHz),
  `tool`, `done`, `error` et `busy` ; fermer la réponse annule le Tour. Sans clé Gemini,
  la route répond toujours `409 installation_incomplete`. Un Tour ne crée aucune Session :
  `owner_client_id` reste vide et le noyau revient au Repos entre deux Tours. Un seul Tour
  à la fois : un Tour pendant une Session (ou l'inverse) est refusé avec un motif lisible,
  côté Console par `busy`, côté Client par le message `Error` existant.
- `GET /api/thread` lit le **Journal** (origine `text`, `voice` ou `event`, rôle, texte,
  horodatage ISO UTC, `retention_days`) et pagine par curseur `before` — le `rowid` du
  Journal, jamais `created_at` qui n'a qu'une seconde de précision.
- Précédence : **environnement du processus > magasin > `.env` > défauts**, avec la source
  de chaque champ (`server/core/settings.py`). L'environnement masque un champ en lecture
  seule ; les chemins des bases, des Plugins et des journaux sont en lecture seule.
- Sans clé Gemini, le serveur démarre en **Installation** : transport des Clients, Webhook
  et Gemini ne sont pas démarrés ; la Console, les Réglages et le Diagnostic répondent.
  `GEMINI_API_KEY` n'est plus obligatoire au démarrage.
- **La fin de l'Installation s'applique sans nouvelle action** : enregistrer la première
  clé Gemini utilisable ouvre à chaud, dans le processus vivant, le transport des Clients,
  le Webhook, Gemini, le juge et la Consolidation ; `restart_required` retombe à `false` et
  la Conversation répond immédiatement. Une clé **remplacée** après installation, elle,
  attend un redémarrage (`restart_required` vrai) : la Connexion Live garde celle chargée
  au démarrage, et la Conversation reste utilisable avec l'ancienne jusque-là.

### Page locale de couplage

Le daemon du Client desktop (`python -m client`) sert une page de couplage en boucle locale
avant même de joindre Athena. On y saisit l'URL du serveur et le Jeton d'accès, on voit
l'état du micro, du Wake word et de la connexion, et on peut demander une reconnexion. Un
Jeton refusé, un serveur injoignable ou un micro absent laissent la page en vie avec une
explication : le daemon ne sort plus faute de Jeton, et un Jeton invalide se corrige sans
redémarrer le processus.

- `ATHENA_CLIENT_HOST` / `ATHENA_CLIENT_PORT` : `127.0.0.1:8768` par défaut. Ne pas ouvrir
  au réseau : la page écrit un Jeton.
- `ATHENA_CLIENT_CONFIG` : fichier de couplage, `$XDG_CONFIG_HOME/athena/client.json`
  (`~/.config/athena/client.json`) par défaut, écrit en `0600` dans un dossier `0700`.
- Précédence : environnement (`ATHENA_ACCESS_TOKEN`, `WS_SERVER_URL`) > fichier > défaut
  `ws://localhost:8765`. Un champ imposé par l'environnement est en lecture seule dans la
  page, qui en affiche la provenance.
- La page refuse `403` tout `Host` non local et tout `Origin` qui ne correspond pas à
  l'hôte de la requête. Le Jeton n'apparaît ni dans le HTML, ni dans `GET /api/state`, ni
  dans la réponse d'un couplage : l'état public est une liste blanche de champs, le Jeton
  n'y figure jamais.

Routes : `GET /` (page autonome, sans ressource externe), `GET /api/state`,
`POST /api/coupling` (URL, Jeton facultatif — vide conserve le Jeton enregistré),
`POST /api/reconnect`.

### Home Assistant

Le plugin `plugins/homeassistant/` pilote la domotique via le serveur MCP natif de Home Assistant.
Activation, secret, configuration et démarrage dégradé : `plugins/homeassistant/README.md` ; décisions
et vocabulaire : `plugins/homeassistant/docs/adr/0001-home-assistant-via-mcp.md`, `plugins/homeassistant/GLOSSARY.md`.

### Créer un plugin

La forme obligatoire d'un dossier de plugin — manifeste, entrypoint, README, glossaire,
tests, smoke test, ADR locaux — et son critère d'amovibilité sont dans
`plugins/README.md`. Le skill `create-plugin` (`.agents/skills/create-plugin/SKILL.md`)
la produit : il demande le nom et le but, délègue le grill à `grill-with-docs`, puis
produit une spec et des tickets, sans écrire de code. Le contrat public est `plugin_api`
(jamais `server`) ; les décisions centrales sont ADR-0014 et ADR-0023. Les skills tiers
sont installés par `npx skills add` et épinglés dans `skills-lock.json`.

### Gérer les plugins depuis la Console

La zone Plugins lit **tous** les dossiers du répertoire des Plugins, y compris ceux
qui ne sont pas listés ou dont le manifeste est cassé : `GET /api/plugins` rend, pour
chacun, l'état, la version, la plateforme, la description, les Tools réellement exposés,
l'instruction d'usage et les champs de configuration et de Secrets **déclarés** par le
manifeste. Un manifeste illisible est une raison lisible dans la réponse, jamais un
démarrage raté.

- `PUT /api/plugins/{nom}` écrit l'activation et la configuration non secrète, validée
  contre les champs du manifeste (URL `http(s)`, champ requis) avec une erreur par champ.
- `PUT|DELETE /api/plugins/{nom}/secrets/{clé}` écrit ou efface un Secret, en écriture
  seule : la réponse ne reprend jamais la valeur, seulement le nom de la variable
  attendue (`ATHENA_PLUGIN_…`), `configured` et la date de mise à jour.
- Les écritures vont dans le fichier d'état (`plugins.json`, non versionné), de façon
  atomique et en préservant les clés inconnues ; elles **prennent effet au redémarrage**,
  rien n'est rechargé à chaud. La réponse signale `restart_required`.
- Aucune installation ni désinstallation : le store de Plugins reste hors périmètre, et
  la Console ne propose aucun bouton d'installation.

### Planificateur

Le **Planificateur** (ADR-0028) est un composant du noyau : il détient les **Planifications**
(un contenu figé et une règle structurée : ponctuelle, intervalle, quotidienne ou
hebdomadaire), les persiste dans une base **dédiée** et les fera entrer dans le Canal
d'événements sous forme d'**Occurrences**. Cette tranche (#194) livre le magasin, le calcul
pur de la prochaine occurrence et deux Tools du noyau : `create_schedule(content, when)` et
`list_schedules()`. Le pump, l'émission et l'Annonce arrivent avec #195 ; `update_schedule` et
`delete_schedule` avec #197. Aucune surface Console n'écrit de Planification.

- `ATHENA_SCHEDULE_DB` : `schedule.db` par défaut (chemin en lecture seule dans les
  Réglages) ; base SQLite dédiée, `CREATE TABLE IF NOT EXISTS`, connexion paresseuse, aucun
  répertoire parent créé. L'Image la place dans `/data`.
- Le fuseau d'interprétation est le Réglage `timezone` (`ATHENA_TIMEZONE`, nom IANA, défaut :
  fuseau du processus) ; les instants sont stockés en UTC à la seconde.
- `when` accepte exactement : `{kind:"once", delay_seconds}` ou `{kind:"once", at:""}`
  (heure locale `YYYY-MM-DDTHH:MM:SS`), `{kind:"interval", every_seconds}`,
  `{kind:"daily", time:"HH:MM"}` et `{kind:"weekly", days:[1..7], time:"HH:MM"}`.
- Bornes du noyau : 200 Planifications, intervalle ≥ 60 s, contenu ≤ 4096 caractères,
  ponctuelle strictement future. Le contenu est une **donnée** : jamais exécuté, jamais lu
  mot pour mot comme instruction.
- Le Diagnostic publie la taille de la base `schedule` sous le libellé « Planifications ».
- Changement d'heure : un créneau local inexistant est sauté, un créneau doublé n'est
  calculé qu'une fois.

### Recherche web

Le plugin `plugins/websearch/` cherche sur le web via Tavily et expose à Gemini un seul Tool, `web_search`.
Activation, secret, options et démarrage dégradé : `plugins/websearch/README.md` ; décisions
et vocabulaire : `plugins/websearch/docs/adr/0001-recherche-web-par-plugin.md`, `plugins/websearch/GLOSSARY.md`.

### Image et déploiement

L'Image Docker est le seul artefact déployable : **un** process qui porte les trois Surfaces,
l'état vivant hors de l'image. Le `Dockerfile` multi-stage construit le build de la SPA, le
code protobuf généré, et fige le modèle d'embedding ; les chemins d'état (`/data`, `/logs`)
et le bind `0.0.0.0` des Surfaces sont des `ENV` de l'image, pas des variables
d'orchestration. Le contrat avec l'orchestrateur (variables à fournir, UID `1000`, labels
Traefik, pièges de routage) est `docs/deploiement.md` — à lire avant de toucher au
`Dockerfile` ou à la configuration de déploiement.

### Pages Notion

Le plugin `plugins/notion/` expose six Tools de page adossés à la REST Notion.
Activation, secret, configuration et périmètre : `plugins/notion/README.md` ; décisions
et vocabulaire : `plugins/notion/docs/adr/0001-notion-en-rest-plutot-que-mcp.md`, `plugins/notion/GLOSSARY.md`.

### Google Workspace

Le plugin `plugins/google-workspace/` expose neuf Tools Gmail, Google Calendar et carnet via l'API People.
Activation, secrets, comptes, panne partielle et reprise : `plugins/google-workspace/README.md` ; décisions
et vocabulaire : `plugins/google-workspace/docs/adr/0001-google-workspace-par-refresh-tokens.md`, `plugins/google-workspace/GLOSSARY.md`.

### Front (`web/`)

Le front est une SPA Vite + React + TypeScript strict, servie en statique par la Console.
Le build n'est jamais committé : `node_modules/` et `web/dist/` sont ignorés.

- `make web` installe les dépendances si besoin (`npm ci`) puis construit (`npm run build`).
- `make web-check` vérifie les types (`tsc --noEmit`), `make web-test` lance `vitest run`.
- En développement, `npm run dev` dans `web/` proxifie `/api` et le WebSocket vers la
  Console (`127.0.0.1:8767` par défaut, port surchargeable par `ATHENA_CONSOLE_PORT`).

La coquille vit dans `web/src/app/` (routeur, identité, palette de commandes), les quatre
zones dans `web/src/modules/` et la couche partagée dans `web/src/api/` (client JSON,
`ApiError`, analyseur SSE, TanStack Query) et `web/src/styles/` (tokens sombre/clair).
Les autres tickets remplissent un module sans réécrire la coquille : chaque zone a sa
route (`/conversation`, `/settings`, `/plugins`, `/diagnostic`), son état vide et son
fichier. Une erreur d'API ne s'affiche jamais brute : tout code stable passe par
`describeError` (`web/src/api/errors.ts`), qui centralise les phrases françaises.
Le Ticket Console seul authentifie (cookie `HttpOnly`) ; un `401` de requête ramène à
`/login` en conservant la route demandée, et les brouillons de saisie libres (nom d'un
Jeton, source, etc.) passent par `useDraft` (stockage de l'onglet) plutôt que par un état
global. Deux exceptions assumées : les formulaires **dérivés du serveur** (Réglages,
configuration d'un Plugin) gardent un état local qui repart de la signature serveur à
chaque lecture, et le mot de passe ne passe jamais par `useDraft`.

L'Installation vit dans `web/src/installation/` : parcours guidé Compte → clé maître
(affichée une seule fois) → clé Gemini (validée, passable) → voix et profil (valeurs
effectives lues par `GET /api/settings`, passable) → récapitulatif, bannière permanente tant
que la clé manque, et Conversation remplacée par un état désactivé. Enregistrer la première
clé active les surfaces sans redémarrage : le parcours mène directement à une Conversation
utilisable. La clé maître ne quitte jamais l'état mémoire de l'écran qui l'affiche ; la
longueur minimale du mot de passe vient de `/api/session`. Une fois l'Installation terminée,
l'`IdentityGate` route vers l'application et `/installation` se redirige.

La Console expose aussi un panneau **Compte** dans la zone Réglages : se déconnecter,
« déconnecter partout » (confirmation explicite ; tous les Tickets du Compte, y compris
celui-ci, sont révoqués) et changer le mot de passe (actuel + nouveau + confirmation,
longueur minimale lue depuis `/api/session` ; les autres navigateurs sont déconnectés).

### Parcours navigateur (E2E)

Quatre parcours Playwright ferment le filet de la Console. Ils tournent sur le **build réel**
(`web/dist`) servi par le serveur du seam partagé (`server/tests/server_harness.py`), avec
un faux modèle injecté : Installation jusqu'à la Conversation, connexion puis un Tour texte
avec audio et interruption, un réglage appliqué jusqu'au redémarrage, et enfin la
conversation vocale avec un micro simulé. Aucun secret réel et aucun réseau externe : le
validateur de Secrets et le modèle sont ceux du harnais.

- `make e2e` construit la SPA, démarre un serveur de test isolé (répertoire temporaire,
  ports éphémères, clé Gemini factice) et lance Playwright depuis `web/`.
- Le parcours vocal rejoue `client/tests/data/athena.wav` par le micro factice de Chromium :
  aucune machine n'a besoin d'un périphérique et aucune permission n'est demandée. Le faux
  modèle y répond par une transcription d'entrée déterministe, une réponse texte et un
  audio ; le serveur de contrôle peut aussi faire tenir une Session à un Client desktop
  pour prouver le refus sans préemption.
- Le superviseur `e2e/server.py` relance le serveur à la même adresse après chaque sortie
  propre (`ATHENA_RESTART_ON_APPLY=1`) : c'est ce qui rend le redémarrage d'un réglage
  observable. Un petit serveur de contrôle, propre au harnais, expose la génération courante
  et permet de demander un redémarrage (le parcours 3 s'en sert ; le parcours 1 prouve au
  contraire que la première clé n'en demande aucun).
- Chaque parcours démarre **son** serveur : l'ordre d'exécution n'a aucune importance.
- Chromium : le système (`/usr/bin/chromium`) est utilisé s'il existe, sinon lancez une fois
  `cd web && npx playwright install chromium`. En CI, le job `e2e` installe Chromium avec
  `npx playwright install --with-deps chromium`.
- `ATHENA_E2E_PYTHON` choisit l'interpréteur Python (défaut : `python`) ;
  `ATHENA_E2E_CHROMIUM` impose un exécutable Chromium.
- Les tests vivent dans `web/e2e/` et les valeurs partagées avec le serveur de test dans
  `web/e2e/constants.ts` (miroir de `e2e/server.py`).
