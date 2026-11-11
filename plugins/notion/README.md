# Plugin Notion

Le plugin `notion` branche Athena sur les **pages Notion** par la **REST Notion**
(ADR local [0001](docs/adr/0001-notion-en-rest-plutot-que-mcp.md)). Il expose à Gemini
six Tools de page et ne passe pas par MCP : Athena parle directement à la REST avec un
jeton d'intégration interne statique. Gemini ne voit jamais Notion, seulement les Tools.

Les six Tools sont `search_pages`, `read_page`, `create_page`, `update_page`,
`trash_page` et `restore_page`. Ils lisent et écrivent du **Markdown** : `read_page` rend
les blocs de premier niveau par pagination, `create_page` et `update_page` acceptent un
corps Markdown. `update_page` **ajoute** par défaut et ne remplace le corps qu'après une
confirmation explicite. `trash_page` exige lui aussi `confirm: true` et annonce les
sous-pages emportées ; `restore_page` annule une mise à la corbeille.

Le plugin épingle `Notion-Version: 2026-03-11` et demande explicitement
`markdown_version: "v2"` sur les trois endpoints Markdown (lecture, création, mise à
jour). La version d'API est une constante de code, jamais une clé de configuration.

**Périmètre.** La connexion ne voit que les pages **partagées avec elle**, et rien
d'autre : le partage est héréditaire vers le bas, une page hors de toute racine partagée
reste invisible. Aucune configuration n'élargit ce périmètre : `parent_page_id` ne
choisit que le parent **par défaut** des créations, et `base_url` ne change que l'adresse
du serveur. `search_pages` ne cherche que des **titres** ; le filtre `query` de
`read_page` ne couvre qu'**une page à la fois**. Il n'existe aucune recherche de contenu
dans tout le workspace, et Athena n'indexe rien localement.

Le plugin n'émet **aucun Événement** : c'est une intégration à la demande, pas un
Watcher ni une Source. Pendant une Annonce proactive, aucun Tool Notion n'est déclaré :
un contenu externe ne peut donc pas déclencher une écriture.

## Activation

Le dossier est versionné, mais **rien ne s'exécute tant qu'il n'est pas listé** dans le
fichier d'état local `plugins.json` (non versionné ; `ATHENA_PLUGINS_STATE`,
`/data/plugins.json` dans l'Image). L'activation est lue au démarrage : il n'y a pas
d'activation à chaud.

```json
{
  "enabled": ["notion"],
  "config": {
    "notion": { "parent_page_id": "0123456789abcdef0123456789abcdef" }
  }
}
```

`parent_page_id` est **requis** et accepte un identifiant (avec ou sans tirets) ou l'URL
entière de la page. La page parente doit être **partagée avec la connexion**, sinon le
démarrage échoue en nommant `parent_page_id`. `base_url` est la seule autre clé déclarée
par le manifeste ; elle reste **optionnelle** (`https://api.notion.com` par défaut) et ne
sert qu'aux tests ou à un proxy.

## Secret

Le manifeste déclare un secret **requis** : `ATHENA_PLUGIN_NOTION_TOKEN`, qui porte un
**jeton d'intégration interne** Notion. Il se fournit dans l'environnement ou dans `.env`
(une variable de processus du même nom l'écrase) :

```dotenv
ATHENA_PLUGIN_NOTION_TOKEN=votre-jeton-notion
```

Absent, il fait échouer le démarrage en nommant la variable, avec le code de sortie `2` :
un secret requis n'est jamais toléré. Le jeton part en `Authorization: Bearer` à chaque
requête et n'apparaît ni dans une Trace, ni dans le journal, ni dans l'instantané de la
Console.

## Prérequis côté Notion

- une **connexion interne** créée pour Athena (<https://www.notion.so/my-integrations>) ;
- les capacités **Lire le contenu**, **Mettre à jour le contenu** et **Insérer le
  contenu** activées sur cette connexion ;
- une page racine **partagée** avec la connexion (menu `•••` de la page → **Connexions**)
  et son identifiant relevé dans l'URL.

Sans la capacité **Lire le contenu**, la sonde de la page parente échoue au démarrage ;
sans les droits d'écriture, le plugin démarre mais chaque écriture rend une phrase `403`.
Le partage d'une page est indépendant des capacités : les capacités seules ne donnent
accès à aucun contenu.

## Démarrage, panne et reprise

Au démarrage, `create()` sonde `GET /v1/users/me` puis la page parente configurée, avec
un budget de **10 s par appel**. L'état est tracé :

```
INFO [plugins] PluginState {"plugin": "notion", "state": "activated", "tools": 6}
```

Un jeton absent, un `parent_page_id` absent ou invalide et une page parente non partagée
(`403`/`404`) sont des défauts de **configuration** : le démarrage échoue en nommant la
clé fautive.

**Démarrage dégradé.** Un jeton refusé (`401`/`403` sur la sonde du compte) ou un Notion
injoignable ne sont pas fatals : `create()` lève `PluginUnavailable`, la Console montre
le plugin `failed` et Athena démarre quand même, sans les Tools. Le serveur retente
toutes les **5 s** et s'active seul dès que Notion répond ; aucun redémarrage d'Athena
n'est nécessaire. Un `5xx` sur l'une des deux sondes n'empêche pas l'activation : Notion
répond, c'est sa sonde qui est en défaut, et le plugin trace `ProbeDegraded`.

**Panne en cours de Session.** Un appel qui échoue (Notion injoignable, délai dépassé,
refus, réponse illisible) ne lève jamais : il rend au modèle une phrase française qui
explique le refus, et la Session survit. Aucun appel n'est rejoué automatiquement. Un
`503`/`504` sur une écriture est traité comme **incertain** : le modèle est invité à
relire la page avant de réessayer. Un Markdown `v2` invalide ou un remplacement qui
supprimerait une sous-page sont refusés sans rien perdre.

## Documentation locale

- [Smoke test du plugin](docs/smoke-test.md) — le parcours manuel de bout en bout.
- [ADR local 0001](docs/adr/0001-notion-en-rest-plutot-que-mcp.md) — REST plutôt que MCP.
- [Glossaire du plugin](GLOSSARY.md) — *Page Notion*.
- [Tests](tests/test_plugins_notion.py), [harnais](tests/notion_harness.py) et
  [faux serveur REST](tests/notion_stub.py).
