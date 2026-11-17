# Smoke test — plugin Websearch

Vérification manuelle de bout en bout du plugin `websearch` : activer le plugin, fournir la
clé Tavily, poser une question qui déclenche le Tool `web_search`, relire les Traces de la
recherche et des Extraits, puis éprouver le quota épuisé et le démarrage dégradé avec sa
relance automatique. Ce document est **la référence** de ce parcours : il ne dépend pas du
smoke test générique du système de plugins.

Pour l'installation d'Athena, la création d'un Compte et de ses Jetons d'accès, voir
`docs/smoke-test-t1-composition.md`.

## Prérequis

Toutes les commandes se lancent **depuis la racine du dépôt** (le python de la venv locale
s'écrit donc `.venv/bin/python`).

- Un `GEMINI_API_KEY` valide (modèle Live).
- Le protocole généré : `.venv/bin/python protocol/proto/generate.py` (ou `make generate`).
- Un micro et un haut-parleur accessibles au client (`pyaudio`).
- Les dépendances à jour : `uv sync`.
- Une **clé Tavily** valide et un compte non épuisé (le plan gratuit suffit).
- Un accès réseau sortant vers `https://api.tavily.com` (ou vers le proxy configuré).
- Un **Jeton d'accès** Athena de nature `client` pour lancer le client desktop. Voir
  `docs/smoke-test-t1-composition.md`. Ne pas le confondre avec la clé Tavily du plugin.

### Lire les traces

Les extraits ci-dessous sont rendus **sans l'horodatage**, avec les clés du payload triées
comme le fait le rendu (`render` trie les clés) et les identifiants abrégés par `…`.
`N` désigne un nombre qui dépend du compte Tavily ou de la réponse du moteur : ce n'est
**pas** une valeur à retrouver à l'identique. Les lignes `DEBUG` n'apparaissent qu'avec
`ATHENA_DEBUG_DECISIONS=1`.

## 1. Activer le plugin et fournir la clé

Le dossier du plugin (`plugins/websearch/`, avec son `plugin.json` et son `plugin.py`) est
**déjà dans le dépôt**. Il ne reste qu'à l'activer et à fournir son secret. La référence
d'activation et de secret est `plugins/websearch/README.md`.

Créez `plugins.json` **à la racine du dépôt** (fichier non versionné, à ne pas commiter) :

```json
{
  "enabled": ["websearch"],
  "config": { "websearch": {} }
}
```

Les cinq options (`base_url`, `search_depth`, `language`, `country`, `strict_language`) sont
facultatives : laissées vides, le plugin applique ses défauts. Pour ce parcours, gardez les
défauts, sauf si vous passez par un proxy (`base_url`).

Créez ou complétez `.env` à la racine (lu par le serveur, et ignoré par git) :

```dotenv
GEMINI_API_KEY=votre-cle
ATHENA_USER_ID=owner
# Clé Tavily du plugin. Une variable de processus du même nom l'écrase.
ATHENA_PLUGIN_WEBSEARCH_API_KEY=tvly-votre-cle
# Verbeux : indispensable pour voir ToolCall, ToolResult et Search
ATHENA_DEBUG_DECISIONS=1
# Emplacements des plugins (défauts : plugins/ et plugins.json)
# ATHENA_PLUGINS_DIR=plugins
# ATHENA_PLUGINS_STATE=plugins.json
# ATHENA_LOG_DIR=logs
```

Le nom de la variable de secret est dérivé du manifeste :
`ATHENA_PLUGIN_<PLUGIN>_<SECRET>` → `ATHENA_PLUGIN_WEBSEARCH_API_KEY`. Une clé valide ne doit
jamais être collée dans `plugins.json`, dans un test ou dans un rapport.

## 2. Démarrer le serveur

```sh
.venv/bin/python -m server
```

Les plugins sont chargés **avant** le transport et avant la trace `started`. La sonde
`GET /usage` valide la clé sans consommer de crédit et tient dans un budget de 8 s.

Attendu, dans cet ordre (source `plugins`) :

```
INFO [plugins] CreditsRemaining {"limit": N, "plugin": "websearch", "remaining": N, "usage": N}
INFO [plugins] PluginState {"plugin": "websearch", "state": "activated", "tools": 1}
...
INFO [app] started {"state": "Idle"}
```

- `CreditsRemaining` n'apparaît que si Tavily expose `usage` et `limit`. Absents, la Trace
  passe en `DEBUG` sous le nom `CreditsUnknown` : le plugin démarre quand même.
- `remaining` est `limit - usage`, jamais négatif. S'il vaut zéro, une Trace
  `CreditsExhausted` de niveau `WARNING` suit immédiatement. Le plugin reste `activated` :
  c'est le moteur qui refusera les recherches suivantes.
- `PluginState` porte `"tools": 1` : le seul Tool du plugin est `web_search`.

## 3. Première recherche

Dans un second terminal :

```sh
export ATHENA_ACCESS_TOKEN=ath_…   # Jeton de nature client
.venv/bin/python -m client
```

Appuyez sur **Entrée** pour ouvrir une Session, puis posez une question factuelle ou
d'actualité, par exemple : « **Qui a gagné le tournoi de tennis de X en 2026 ?** ».

Attendu à l'oral : Athena annonce en une phrase qu'elle cherche, puis répond en attribuant
la source par son nom — sans lire d'URL et sans réciter d'extrait brut. Dans le terminal du
serveur (avec `ATHENA_DEBUG_DECISIONS=1`) :

```
INFO  [core] ToolCall {"arguments": {"query": "…"}, "name": "web_search", …}
INFO  [plugins] Search {"cache": "miss", "credits": 1, "duration_ms": …, "params": {"chunks_per_source": 2, "country": "france", "language": "fr", "max_results": 10, "search_depth": "basic", "topic": "general"}, "plugin": "websearch", "query": "…", "results": N}
INFO  [core] ToolResult {"name": "web_search", "payload": {"count": N, "results": [{"excerpt": "…", "title": "…", "url": "…"}, …], "status": "ok"}, …}
```

- `Search` trace la requête, les paramètres **effectivement partis**, la durée, le nombre de
  résultats, le cache et les crédits consommés (un pour `basic` et `fast`, deux pour
  `advanced`).
- Le Tool rend des **Extraits de recherche** : titre, URL, extrait, et `published_date`
  seulement quand le moteur la fournit. Une réponse sans aucun résultat est un succès
  (`"count": 0`), pas une erreur.
- La clé n'apparaît dans **aucune** ligne : ni `Search`, ni `ToolCall`, ni `ToolResult`, ni
  `logs/server.log`, ni l'instantané de la Console. Vérifiez-le : `grep` de la clé dans
  `logs/` ne doit rien rendre.

## 4. Répéter la même question

Reposez **exactement** la même question dans la même Session. Le plugin sert la charge
depuis son cache (TTL court, clé = requête normalisée + paramètres) :

```
INFO [plugins] Search {"cache": "hit", "credits": 0, …, "query": "…", "results": N}
```

Aucun second appel au moteur, aucun crédit consommé. Une question reformulée seulement par
la casse, les espaces ou la ponctuation finale retombe sur la même entrée.

## 5. Quota épuisé

Pour éprouver le quota sans attendre la fin du mois, utilisez un compte Tavily dont
`usage` a atteint `limit`, ou pointez `base_url` vers un moteur compatible qui renvoie
`{"usage": N, "limit": N}` sur `GET /usage`. Attendu au démarrage :

```
INFO    [plugins] CreditsRemaining {"limit": N, "plugin": "websearch", "remaining": 0, "usage": N}
WARNING [plugins] CreditsExhausted {"limit": N, "plugin": "websearch", "remaining": 0, "usage": N}
INFO    [plugins] PluginState {"plugin": "websearch", "state": "activated", "tools": 1}
```

Le plugin reste actif. Une recherche part alors au moteur et, s'il la refuse, rend au modèle
une phrase française (`La recherche web a été refusée par le moteur. Vérifie la clé d'API,
puis réessaie.`) : la Session survit, et la Trace `Search` porte `"status": "error"` avec
`"credits": 0`.

## 6. Démarrage dégradé et relance automatique

Deux pannes réparables donnent le même état `failed` sans tuer Athena : une clé refusée
(`401`/`403`) et un moteur injoignable. Pour la première, remplacez temporairement la clé
par une clé invalide dans `.env` et redémarrez :

```
WARNING [plugins] ProbeFailed {"plugin": "websearch", "status": 401}
INFO    [plugins] PluginState {"plugin": "websearch", "reason": "PluginUnavailable: websearch probe failed: the engine refused the API key (HTTP 401)", "state": "failed"}
INFO    [app] started {"state": "Idle"}
```

Attendu :

- Athena démarre quand même ; la Console montre le plugin `failed` et aucun Tool
  `web_search` n'est déclaré dans la Session (seuls les Tools du noyau le sont).
- La retente de fond réessaie toutes les **5 s** :
  `INFO [plugins] PluginState {"plugin": "websearch", "reason": "…", "retry": true, "state": "failed"}`.
- Une sonde `5xx` n'est **pas** une panne du plugin : le moteur répond, sa sonde est en
  défaut. La Trace est `ProbeDegraded` et le plugin s'active.

Pour observer la **reprise sans redémarrer Athena**, rendez le moteur à nouveau joignable.
Avec un vrai Tavily, une clé refusée ne se répare qu'en fournissant la bonne clé, donc
utilisez un `base_url` local (proxy ou moteur compatible) que vous arrêtez puis relancez,
ou coupez puis rétablissez l'accès réseau de la machine. Attendu à la première retente
réussie :

```
INFO [plugins] PluginState {"plugin": "websearch", "retry": true, "state": "activated", "tools": 1}
```

Le Tool est alors déclaré à la **Connexion Live suivante** : rouvrez une Session, sans
redémarrer le serveur.

## 7. Panne en cours de Session

Moteur lancé et plugin `activated`, coupez le moteur (ou le réseau) puis posez une question.
Attendu : Athena dit simplement que la recherche n'a pas abouti et propose de réessayer ;
la Session continue, le plugin reste `activated`. Le Tool ne lève jamais et rend une phrase
française au modèle :

```
INFO [plugins] Search {"cache": "miss", "credits": 0, "duration_ms": …, "params": {…}, "plugin": "websearch", "query": "…", "reason": "ConnectError", "results": 0, "status": "error"}
```

Un délai dépassé rend une phrase distincte (`le moteur n'a pas répondu à temps`), une
réponse illisible une troisième (`le moteur a renvoyé une réponse illisible`). Aucune
erreur n'entre au cache : la même question réussit dès que le moteur revient.

## Limites connues

- **Pas d'activation à chaud** : `plugins.json` n'est lu qu'au démarrage.
- **Mauvais secret** : un secret **manquant** est fatal (code de sortie `2`, variable
  nommée) ; une clé **refusée** n'est pas fatale et donne le démarrage dégradé ci-dessus.
- **Pas de recherche pendant une Annonce** : une Session d'annonce déclare une liste de
  Tools vide ; Athena ne cherche pas pour appuyer une annonce proactive.
- **Un crédit par recherche non cachée** : `advanced` en coûte deux. Le cache et la Trace
  `Search` sont la seule défense contre la surprise en fin de mois.
- **Les requêtes sortent de la maison** : chaque recherche part chez Tavily avec la clé de
  l'instance ; c'est pourquoi la Trace nomme la requête.
- **Pas d'Événement** : le plugin ne pousse rien vers le noyau.

## Vérification automatisée

```sh
.venv/bin/python -m pytest plugins/websearch/tests -q
```

Les scénarios ci-dessus s'appuient sur :

- `plugins/websearch/tests/test_plugins_websearch.py` — le **vrai** plugin contre un faux
  moteur Tavily en processus (`plugins/websearch/tests/tavily_stub.py`) : manifeste, secret
  manquant, sonde refusée, injoignable et `5xx`, quota épuisé, déclaration et schéma du
  Tool, Extraits projetés, paramètres envoyés, cache, budgets d'extraits, pannes rendues en
  phrases, relance automatique, clé jamais tracée ;
- `server/tests/test_plugins.py` — agrégation du registre, collision de noms, attribution du
  propriétaire, composition de l'instruction ;
- `server/tests/test_plugins_loader.py` — états `activated` / `failed` / `missing` / `inactive` /
  `unsupported` / `invalid`, retente, cas fatals, expurgation des secrets ;
- `server/tests/test_plugins_instruction.py` — instruction dans les Sessions ordinaires et
  les Annonces, et taille (jamais le texte) dans `Activation` ;
- `server/tests/test_app_integration.py` — couture application : `server_tools` peuplé, Tools
  de plugin déclarés et appelés, plugin désactivé, retente dégradée prise en compte à la
  Connexion Live suivante ;
- `server/tests/test_transport_server.py` — `Registered.server_tools` et relecture du
  fournisseur à chaque enregistrement ;
- `server/tests/test_server_main.py` — code de sortie 2 sur plugin cassé ;
- `server/tests/test_app_secrets.py` — secrets de plugin lus depuis `.env`, priorité à
  l'environnement du processus.

## Résultat du run

**Non exécuté à ce jour.** Ce document décrit le comportement **implémenté**, couvert par les
tests listés ci-dessus ; les `N`, les durées et les résultats dépendent d'un vrai compte
Tavily et restent à relever. Ce document n'est donc **pas** un compte rendu de run.
