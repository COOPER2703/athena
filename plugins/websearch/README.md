# Plugin Websearch

Le plugin `websearch` branche Athena sur le **moteur de recherche Tavily** (ADR local
[0001](docs/adr/0001-recherche-web-par-plugin.md)). Il expose à Gemini un seul Tool,
`web_search`, qui interroge le web et rend des **Extraits de recherche** : titre, URL,
extrait court, date de publication quand elle existe. Gemini reste le seul rédacteur de la
réponse vocale ; Athena ne lui livre jamais une page entière.

Le plugin n'émet aucun Événement et ne pousse rien : la recherche n'existe qu'en Session,
jamais pendant une Annonce proactive.

## Activation

Le dossier est versionné, mais **rien ne s'exécute tant qu'il n'est pas listé** dans le
fichier d'état local `plugins.json` (non versionné ; `ATHENA_PLUGINS_STATE`,
`/data/plugins.json` dans l'Image). L'activation est lue au démarrage : il n'y a pas
d'activation à chaud.

```json
{
  "enabled": ["websearch"],
  "config": { "websearch": {} }
}
```

## Options

Les cinq options sont **facultatives** et se placent sous `config.websearch` ; le plugin
applique lui-même les défauts quand elles manquent :

- `base_url` : `https://api.tavily.com` — proxy ou moteur compatible ;
- `search_depth` : `basic` — `fast`, `basic` ou `advanced` (`advanced` coûte deux crédits,
  les deux autres un seul) ;
- `language` : `fr` — langue du biais de classement ;
- `country` : `france` — envoyé seulement pour `topic: general`, Tavily le refuse ailleurs ;
- `strict_language` : `0` — `1` filtre strictement la langue au lieu de la biaiser.

Une profondeur inconnue retombe sur `basic` : une faute de frappe dans `plugins.json` ne
part jamais au moteur et n'invente pas de crédits.

## Secret

Le manifeste déclare un secret **requis** : `ATHENA_PLUGIN_WEBSEARCH_API_KEY`, qui porte une
clé d'API Tavily. Il se fournit dans l'environnement ou dans `.env` (une variable de
processus du même nom l'écrase) :

```dotenv
ATHENA_PLUGIN_WEBSEARCH_API_KEY=tvly-votre-cle
```

Absent, il fait échouer le démarrage en nommant la variable, avec le code de sortie `2` :
un secret requis n'est jamais toléré. La clé part en `Authorization: Bearer` à chaque
requête et n'apparaît ni dans une Trace, ni dans le journal, ni dans l'instantané de la
Console.

## Démarrage, quota et reprise

Au démarrage, `create()` sonde `GET /usage` : la clé est validée **sans consommer de
crédit**. Les compteurs sont tracés quand Tavily les expose :

```
INFO  [plugins] CreditsRemaining {"limit": N, "plugin": "websearch", "remaining": N, "usage": N}
INFO  [plugins] PluginState {"plugin": "websearch", "state": "activated", "tools": 1}
```

Un quota épuisé (`remaining` à zéro) ajoute un avertissement `CreditsExhausted` : le plugin
reste actif, mais les recherches suivantes seront refusées par le moteur.

Une recherche trace ensuite la requête, les paramètres effectivement partis, la durée, le
nombre de résultats, le cache et les crédits consommés. Une question identique encore
fraîche est servie depuis le cache (`"cache": "hit"`) sans second appel ni crédit.

**Démarrage dégradé.** Une clé refusée (`401`/`403`) ou un moteur injoignable n'est pas
fatal : `create()` lève `PluginUnavailable` après une Trace `ProbeFailed`, la Console montre
le plugin `failed` et Athena démarre quand même, sans le Tool. Le serveur retente toutes les
**5 s** et s'active seul dès que le moteur répond ; aucun redémarrage d'Athena n'est
nécessaire. Une sonde `5xx` est différente : le moteur répond, c'est sa sonde qui est en
défaut, le plugin s'active et trace `ProbeDegraded`. Seul un secret **manquant**, une
configuration requise absente ou un manifeste invalide est fatal.

**Panne en cours de Session.** Un appel qui échoue (moteur injoignable, délai dépassé,
requête refusée, réponse illisible) ne lève jamais : il rend au modèle une phrase française
qui explique le refus, et la Session survit. Aucun retry n'est tenté pendant la
conversation, et une erreur n'entre jamais au cache.

## Documentation locale

- [Smoke test du plugin](docs/smoke-test.md) — le parcours manuel de bout en bout.
- [ADR local 0001](docs/adr/0001-recherche-web-par-plugin.md) — les décisions du plugin.
- [Glossaire du plugin](GLOSSARY.md) — *Recherche web* et *Extrait de recherche*.
- [Tests](tests/test_plugins_websearch.py) et [faux moteur Tavily](tests/tavily_stub.py).
