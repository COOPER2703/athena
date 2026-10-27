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

### Front (`web/`)

Le front est une SPA Vite + React + TypeScript strict, servie en statique par la Console.
Le build n'est jamais committé : `node_modules/` et `web/dist/` sont ignorés.

- `make web` installe les dépendances si besoin (`npm ci`) puis construit (`npm run build`).
- `make web-check` vérifie les types (`tsc --noEmit`), `make web-test` lance `vitest run`.
- En développement, `npm run dev` dans `web/` proxifie `/api` et le WebSocket vers la
  Console (`127.0.0.1:8767` par défaut, port surchargeable par `ATHENA_CONSOLE_PORT`).
