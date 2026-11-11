# Carte des glossaires

Ce dépôt est **multi-contexte** : le vocabulaire du noyau et du contrat vit à la racine,
chaque plugin porte le sien dans son dossier. Quand un terme manque à `GLOSSARY.md`, c'est
souvent qu'il appartient à un contexte précis : lisez celui de la zone où vous travaillez.

| Contexte | Glossaire | Décisions |
| --- | --- | --- |
| Noyau et contrat | [`GLOSSARY.md`](GLOSSARY.md) | [`docs/adr/`](docs/adr/README.md) |
| `homeassistant` | [`plugins/homeassistant/GLOSSARY.md`](plugins/homeassistant/GLOSSARY.md) | [`plugins/homeassistant/docs/adr/`](plugins/homeassistant/docs/adr/0001-home-assistant-via-mcp.md) |
| `websearch` | [`plugins/websearch/GLOSSARY.md`](plugins/websearch/GLOSSARY.md) | [`plugins/websearch/docs/adr/`](plugins/websearch/docs/adr/0001-recherche-web-par-plugin.md) |
| `notion` | [`plugins/notion/GLOSSARY.md`](plugins/notion/GLOSSARY.md) | [`plugins/notion/docs/adr/`](plugins/notion/docs/adr/0001-notion-en-rest-plutot-que-mcp.md) |
| `google-workspace` | [`plugins/google-workspace/GLOSSARY.md`](plugins/google-workspace/GLOSSARY.md) | [`plugins/google-workspace/docs/adr/`](plugins/google-workspace/docs/adr/0001-google-workspace-par-refresh-tokens.md) |

Les tickets de migration ajoutent une ligne par plugin au fur et à mesure que son glossaire
quitte la racine. Un plugin sans ligne ici n'a pas encore de vocabulaire propre.
