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
écoute par défaut sur `127.0.0.1:8767`, **activée**, sans authentification en v1. Sa
configuration suit le même schéma que le Webhook :

- `ATHENA_CONSOLE_ENABLED` : `1` par défaut ; `0` désactive complètement la surface.
- `ATHENA_CONSOLE_HOST` : `127.0.0.1` par défaut (loopback ; ne l'ouvrir au réseau qu'en
  connaissance de cause).
- `ATHENA_CONSOLE_PORT` : `8767` par défaut.

Le Webhook reste séparé (`ATHENA_WEBHOOK_ENABLED`, `ATHENA_WEBHOOK_HOST`,
`ATHENA_WEBHOOK_PORT`, `127.0.0.1:8766` par défaut). Le contrat JSON de la Console
commence par `GET /api/status` ; le front vit dans `web/` et n'est pas requis pour que
l'API réponde.
