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

Le Webhook reste séparé (`ATHENA_WEBHOOK_ENABLED`, `ATHENA_WEBHOOK_HOST`,
`ATHENA_WEBHOOK_PORT`, `127.0.0.1:8766` par défaut). Le contrat JSON de la Console
commence par `GET /api/status` ; le front vit dans `web/` et n'est pas requis pour que
l'API réponde.

### Authentification

Le module d'auth vit dans `server/adapters/auth/` (port `AuthService` dans
`server/core/ports.py`). Il possède sa propre base SQLite, **distincte de la mémoire** :
`ATHENA_AUTH_DB` (`auth.db` par défaut, relatif au répertoire de travail). Les tables
(Comptes, Tickets Console, Jetons d'accès) sont créées par `CREATE TABLE IF NOT EXISTS`,
sans migration. Au démarrage sans Compte, un Code d'installation à usage unique est écrit
dans le journal (`logs/server.log`) sous `[auth] InstallationCode` ; il n'est plus jamais
émis dès qu'un Compte existe.

- `ATHENA_CONSOLE_TICKET_DAYS` : `30` jours glissants par défaut.
- `ATHENA_CONSOLE_TICKET_REFRESH_SECONDS` : `3600` (l'expiration n'est repoussée qu'au
  plus une fois par heure).
- `ATHENA_AUTH_PASSWORD_MIN_LENGTH` : `12` caractères minimum, imposés côté serveur
  (hachage Argon2id).
- `ATHENA_AUTH_LOGIN_MAX_ATTEMPTS` : `5` échecs avant blocage.
- `ATHENA_AUTH_LOGIN_BLOCK_MINUTES` : `15` minutes de blocage, levé automatiquement.
- `ATHENA_AUTH_SETUP_MAX_ATTEMPTS` : `5` tentatives d'installation avant blocage.

Les deux mécanismes de credential (ADR-0017) :

- **Ticket Console** : cookie `athena_ticket` (`HttpOnly; Secure; SameSite=Strict`, `Path=/`),
  30 jours glissants. Il authentifie toutes les routes d'API hors identité et le WebSocket du
  flux ; `GET /healthz` reste public.
- **Jeton d'accès** : `Authorization: Bearer ath_…`, nature `client` (transport) ou `source`
  (Webhook). Le transport (`WS_HOST` / `WS_PORT`) refuse le handshake sans Jeton `client` par
  `401` + `WWW-Authenticate: Bearer`, avant toute trame. Le Webhook
  (`ATHENA_WEBHOOK_ENABLED` / `ATHENA_WEBHOOK_HOST` / `ATHENA_WEBHOOK_PORT`) exige un Jeton
  `source` ; sa Source fait autorité et une Source de corps contradictoire est refusée `403`.
- **Client desktop** : lit son Jeton dans `ATHENA_ACCESS_TOKEN` (variable de processus
  uniquement, pas de `.env` client) et refuse de démarrer s'il manque.

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
