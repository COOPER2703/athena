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

### Créer un plugin

Le skill `create-plugin` (`.agents/skills/create-plugin/SKILL.md`) conçoit un nouveau plugin
serveur : il demande le nom et le but, délègue le grill à `grill-with-docs`, puis produit une
spec et des tickets d'implémentation. Il n'écrit aucun code. Se lance avec `/create-plugin`.

Le contrat public est `plugin_api` (jamais `server`) ; les décisions sont ADR-0014 et
ADR-0018 ; les deux exemples sont `plugins/websearch` (HTTP) et `plugins/homeassistant` (MCP).
Les skills tiers sont installés par `npx skills add` et épinglés dans `skills-lock.json`.

### Recherche web

Le plugin `plugins/websearch/` (ADR-0018) expose à Gemini un seul Tool, `web_search`,
adossé à Tavily : il rend des **Extraits de recherche** (titre, URL, extrait, date de
publication quand elle existe) et Gemini reste le seul rédacteur de la réponse. Le dossier
est versionné, mais **rien ne s'exécute tant qu'il n'est pas listé**.

Pour l'activer, ajoutez `websearch` à la liste `enabled` du fichier d'état local
`plugins.json` (non versionné) :

```json
{ "enabled": ["websearch"], "config": { "websearch": {} } }
```

Le manifeste déclare un secret **requis** : `ATHENA_PLUGIN_WEBSEARCH_API_KEY`, lu comme les
autres secrets de plugin (environnement ou `.env`). Absent, le démarrage échoue
explicitement en nommant la variable (code de sortie 2). Cinq options, toutes
**optionnelles**, se placent sous `config.websearch` ; le plugin applique lui-même les
défauts :

- `base_url` : `https://api.tavily.com` (proxy ou moteur compatible) ;
- `search_depth` : `basic` (`fast`, `basic` ou `advanced` ; `advanced` coûte deux crédits) ;
- `language` : `fr` ;
- `country` : `france` (envoyé seulement pour `topic: general`) ;
- `strict_language` : `0` (`1` filtre strictement la langue au lieu de la biaiser).

Au démarrage, `create()` sonde `GET /usage` : la clé est validée sans consommer de crédit,
les crédits restants sont tracés (`CreditsRemaining`) et un quota épuisé avertit
(`CreditsExhausted`). Une clé refusée ou un moteur injoignable n'est pas fatal : le plugin
démarre **dégradé**, la Console le montre en `failed`, et le serveur retente toutes les 5 s ;
il s'active seul quand le moteur revient. Une panne en cours de Session reste une phrase
rendue au modèle, jamais un arrêt. La clé n'apparaît ni dans une Trace, ni dans le journal,
ni dans l'instantané de la Console.

### Image et déploiement

L'Image Docker est le seul artefact déployable : **un** process qui porte les trois Surfaces,
l'état vivant hors de l'image. Le `Dockerfile` multi-stage construit le build de la SPA, le
code protobuf généré, et fige le modèle d'embedding ; les chemins d'état (`/data`, `/logs`)
et le bind `0.0.0.0` des Surfaces sont des `ENV` de l'image, pas des variables
d'orchestration. Le contrat avec l'orchestrateur (variables à fournir, UID `1000`, labels
Traefik, pièges de routage) est `docs/deploiement.md` — à lire avant de toucher au
`Dockerfile` ou à la configuration de déploiement.

### Pages Notion

Le plugin `plugins/notion/` (ADR-0019) expose six Tools de page adossés à la REST Notion :
`search_pages`, `read_page`, `create_page`, `update_page`, `trash_page`, `restore_page`. Il
épingle `Notion-Version: 2026-03-11` et demande `markdown_version: "v2"` explicitement. Il ne
voit que ce qui est partagé avec la connexion : aucune configuration n'élargit ce périmètre,
et la recherche de contenu se limite à une page à la fois.

Pour l'activer, ajoutez `notion` à la liste `enabled` de `plugins.json` avec
`parent_page_id` sous `config.notion`, et fournissez le secret requis
`ATHENA_PLUGIN_NOTION_TOKEN` (environnement ou `.env`) :

```json
{
  "enabled": ["notion"],
  "config": { "notion": { "parent_page_id": "…" } }
}
```

La page parente doit être partagée avec la connexion, sinon le démarrage échoue en nommant
`parent_page_id`. La recette complète (partage, capacités, parcours vocal, dépannage) est
`docs/smoke-test-notion.md`.

### Google Workspace

Le plugin `plugins/google-workspace/` (ADR-0021, ADR-0022) expose neuf Tools adossés à
Gmail, Google Calendar et l'API People : `gmail_search`, `gmail_read`, `gmail_send`,
`gmail_reply` pour les mails, `calendar_list_events`, `calendar_create_event`,
`calendar_update_event`, `calendar_delete_event` pour l'agenda, et `contacts_search` pour
le carnet. Chaque **Compte Google** porte son propre client OAuth ; les refresh tokens sont
échangés en mémoire contre des access tokens d'une heure, jamais persistés. Les envois de
mails et les suppressions d'événements exigent une confirmation explicite. Aucun Événement
proactif n'est poussé en v1.

`contacts_search` interroge les contacts **enregistrés** (`people/me/connections`) et les
**autres contacts** que Gmail a mémorisés (`otherContacts`), fusionnés sur une adresse
identique ; il ne voit ni l'annuaire du domaine, ni les fiches sans adresse. Un nom seul
n'est jamais une adresse : `gmail_send` refuse un destinataire qui n'y ressemble pas et
renvoie vers le Tool. Quand plusieurs adresses ou plusieurs personnes correspondent,
l'instruction impose de les lire et de faire choisir, jamais de trancher. Le carnet n'est
pas mis en cache : chaque recherche refait ses appels.

Pour l'activer, ajoutez `google-workspace` à la liste `enabled` de `plugins.json` avec les
libellés de comptes sous `config.google-workspace`, et fournissez les trois secrets requis
dans l'environnement ou `.env`, chacun une liste **de même longueur et dans le même ordre**
que `accounts` (les valeurs Google ne contiennent pas de virgule) :

```json
{
  "enabled": ["google-workspace"],
  "config": {
    "google-workspace": {
      "accounts": "Pro,perso",
      "default_account": "perso"
    }
  }
}
```

```sh
ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_ID=idPro,idPerso
ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_SECRET=secretPro,secretPerso
ATHENA_PLUGIN_GOOGLE_WORKSPACE_REFRESH_TOKEN=rtPro,rtPerso
```

`default_account` est optionnelle (le premier compte fait foi) ; `api_base_url`,
`people_base_url` et `token_url` sont réservées aux tests ou à un proxy (`people_base_url`
vaut `https://people.googleapis.com` par défaut). Les scopes demandés sont
`gmail.readonly`, `gmail.send`, `calendar.events`, `contacts.readonly` et
`contacts.other.readonly` ; l'API **People** doit être activée dans le projet Cloud. Le
client OAuth est de type « Web application » avec le redirect du Playground, et une
application **External + Testing** est refusée (refresh tokens expirant en 7 jours).

Au démarrage, `create()` échange en parallèle le refresh token de chaque compte. Si au moins
un compte répond, le plugin s'active ; un compte refusé (`invalid_grant`) est tracé
`AccountUnavailable`, reste visible dans la Console et est retenté à **chaque** appel de Tool
qui le vise, jusqu'à une phrase française d'indisponibilité. Un scope Contacts manquant
n'empêche jamais l'activation : chaque compte sain sonde son carnet au démarrage et, en cas
de refus réparable, trace `ContactsScopeMissing` ; le premier appel réel rend une phrase qui
nomme l'API People et la réautorisation. Si tous les comptes échouent, le
plugin rend `PluginUnavailable` : la Console le montre en `failed` et le serveur retente
toutes les 5 s. Ni les Traces, ni le journal, ni l'instantané, ni les messages d'erreur ne
portent un secret, une adresse, un objet, un corps, une requête Gmail ou une URL complète.
La recette complète (client OAuth, Playground, consentement, réautorisation après
révocation, dépannage) est `docs/smoke-test-google-workspace.md`.

### Front (`web/`)

Le front est une SPA Vite + React + TypeScript strict, servie en statique par la Console.
Le build n'est jamais committé : `node_modules/` et `web/dist/` sont ignorés.

- `make web` installe les dépendances si besoin (`npm ci`) puis construit (`npm run build`).
- `make web-check` vérifie les types (`tsc --noEmit`), `make web-test` lance `vitest run`.
- En développement, `npm run dev` dans `web/` proxifie `/api` et le WebSocket vers la
  Console (`127.0.0.1:8767` par défaut, port surchargeable par `ATHENA_CONSOLE_PORT`).
