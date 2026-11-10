# Plugins serveur

Un plugin est une **unité amovible** : tout ce qui n'appartient qu'à lui vit dans son
dossier. Supprimer `plugins/<nom>/` ne doit laisser aucune **référence dure** ailleurs —
un import, un chemin construit, une entrée de build ou de configuration. Une chaîne
**inerte** (un nom de plugin utilisé comme valeur de remplissage dans une fixture du
noyau) n'est pas une référence dure : elle reste tolérée. La décision et ses deux
arbitrages assumés sont l'ADR-0023 ; le contrat et le chargement restent l'ADR-0014.

## Forme obligatoire

```
plugins/<nom>/
├── plugin.json        manifeste (identité, config, secrets) — inchangé
├── plugin.py          entrypoint `create(context)` — inchangé
├── README.md          ce que c'est, comment l'activer, quels secrets fournir
├── GLOSSARY.md        le vocabulaire propre au plugin
├── tests/
│   ├── conftest.py    rend les modules d'aide du dossier importables
│   ├── <nom>_stub.py  faux serveur externe (Tavily, Home Assistant, Notion, Google…)
│   └── test_plugins_<nom>.py
└── docs/
    ├── smoke-test.md  la recette manuelle, lue à côté du code qu'elle vérifie
    └── adr/
        └── 0001-….md  les décisions du plugin, numérotées localement
```

- Le **README** absorbe ce que la documentation centrale disait du plugin : activation,
  variables de secret, options de configuration, démarrage dégradé. Il reste court et
  versionné ; `tests/` et `docs/` ne sont pas embarqués dans l'Image.
- Les **tests** importent `testkit` (le harnais partagé, cinq noms) et leur propre
  dossier. Le `conftest.py` local ajoute ce dossier au chemin ; il n'importe pas celui du
  serveur. Chaque plugin garde des noms de modules d'aide uniques.
- Les **ADR** du plugin se numérotent à partir de `0001`, localement. La correspondance
  ancien numéro central → nouveau numéro local est tenue dans `docs/adr/README.md`.

## Activation

Le fichier d'état reste **hors du dossier** : c'est de l'état de déploiement, hors du
dépôt en production (`ATHENA_PLUGINS_STATE`, `/data/plugins.json` dans l'Image). Déposer
un dossier n'exécute rien. Pour activer :

```json
{
  "enabled": ["<nom>"],
  "config": { "<nom>": { "option": "valeur" } }
}
```

Les options non secrètes vivent sous `config.<nom>` ; elles sont déclarées par le
manifeste et validées au chargement.

## Secrets

Un secret n'est **jamais** une valeur du dossier ni du manifeste : le manifeste déclare
seulement sa clé et son caractère obligatoire. La valeur vient de l'environnement ou du
`.env`, sous la forme :

```
ATHENA_PLUGIN_<NOM>_<CLE>
```

en majuscules, tout caractère non alphanumérique remplacé par `_`. Exemples :
`ATHENA_PLUGIN_WEBSEARCH_API_KEY`, `ATHENA_PLUGIN_HOMEASSISTANT_TOKEN`,
`ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_ID`. Un secret obligatoire absent fait échouer le
démarrage en nommant la variable (code de sortie 2) ; il n'est jamais tracé.
