# Index des ADR

## Numérotation

- Les ADR **du noyau et du système** vivent dans ce dossier et se numérotent au centre,
  à la suite, sur quatre chiffres (`0023-…`). C'est un compteur partagé : un ADR qui
  appartient à un plugin n'y a pas sa place.
- Les ADR **d'un plugin** vivent dans `plugins/<nom>/docs/adr/` et se numérotent
  **localement à partir de `0001`**. Deux plugins peuvent donc porter le même numéro :
  c'est voulu, chaque dossier est un contexte indépendant.
- Quand un ADR de plugin déménage du centre vers son dossier, il est **renuméroté** et la
  correspondance est ajoutée au tableau ci-dessous. Les citations de ces numéros ne
  vivent que dans des documents qui déménagent avec eux ; la renumérotation est donc bon
  marché.

## Correspondance ancien → nouveau

Table des ADR de plugin déplacés du centre vers `plugins/<nom>/docs/adr/`. Elle se
remplit à chaque déménagement (spec #147, tickets #149 à #154).

| Ancien ADR central | Nouvel ADR local | Ticket |
| ------------------ | ---------------- | ------ |
| `0015` | `plugins/homeassistant/docs/adr/0001-home-assistant-via-mcp.md` | #149 |
| `0018` | `plugins/websearch/docs/adr/0001-recherche-web-par-plugin.md` | #150 |
| `0019` | `plugins/notion/docs/adr/0001-notion-en-rest-plutot-que-mcp.md` | #151 |
| `0021` | `plugins/google-workspace/docs/adr/0001-google-workspace-par-refresh-tokens.md` | #152 |
| `0022` | `plugins/google-workspace/docs/adr/0002-contacts-google-via-people-api.md` | #152 |

## ADR du centre

Les décisions du noyau et du système restent listées ici par leur fichier :

- `0023-dossier-de-plugin-unite-amovible.md` — la forme d'un dossier de plugin.
- `0024-tour-unite-de-conversation.md` — le Tour comme unité de conversation.
- `0025-reglages-et-secrets.md` — réglages applicatifs et Secrets chiffrés.
- `0026-installation-sans-code.md` — l'Installation au premier démarrage.
- `0027-console-heberge-un-client-vocal.md` — le Client Console et la conversation vocale.
- `0028-planificateur-evenements-planifies.md` — le Planificateur, ses Occurrences et l'Annonce
  sans jugement.

