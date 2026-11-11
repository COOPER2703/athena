# Smoke test — T4 système de plugins (`python -m server` + Console)

Vérification manuelle du **système de plugins** serveur (ADR-0014, complété par
ADR-0023) : au démarrage, Athena découvre les dossiers de plugins, n'importe que ceux
listés dans le fichier d'état, publie leurs Tools et leur instruction, et rend leur état
lisible dans la Console.

Ce document ne met en scène **aucun plugin réel**. Le parcours s'appuie sur un plugin
synthétique, `fixture`, construit ci-dessous à partir d'un manifeste et d'un court
`plugin.py` — exactement comme le font les tests du chargeur. Il reste donc vrai quand un
plugin réel est retiré du dépôt. Pour un exemple concret, chaque plugin documente son
activation dans son propre `README.md` ; la forme obligatoire d'un dossier et la règle
d'activation sont dans [`plugins/README.md`](../plugins/README.md).

Le parcours couvre la découverte, l'activation, l'absence de fichier d'état, un échec
fatal opposé à un démarrage dégradé avec relance, la collision de nom de Tool et
l'instantané de la Console.

Voir `docs/smoke-test-t1-composition.md` pour l'installation d'Athena et
`docs/smoke-test-t5-console.md` pour le Compte, le Ticket Console et le détail de
`GET /api/status`.

## Prérequis

Toutes les commandes se lancent **depuis la racine du dépôt** (le python de la venv locale
s'écrit donc `.venv/bin/python`).

- Les dépendances à jour : `uv sync`.
- Le protocole généré : `.venv/bin/python protocol/proto/generate.py` (ou `make generate`).
- Le modèle d'embedding local disponible : le serveur le précharge au démarrage.
- Un **Compte** et un **Ticket Console** (ADR-0017), pour lire l'instantané : le premier
  démarrage de l'étape 2 les crée, les étapes suivantes les réutilisent. Le parcours
  n'ouvre aucune Session Live : un `GEMINI_API_KEY` factice suffit, et aucun micro n'est
  nécessaire.
- Aucun service externe, aucun secret de plugin : `fixture` ne déclare ni configuration ni
  secret.

### Isoler le run

```sh
export SMOKE=/tmp/athena-smoke
export ATHENA_PLUGINS_DIR="$SMOKE/plugins"
export ATHENA_PLUGINS_STATE="$SMOKE/plugins.json"
export ATHENA_MEMORY_DB="$SMOKE/memory.db"
export ATHENA_AUTH_DB="$SMOKE/auth.db"
export ATHENA_LOG_DIR="$SMOKE/logs"
export GEMINI_API_KEY=cle-factice
export ATHENA_DEBUG_DECISIONS=1
mkdir -p "$SMOKE/plugins"
```

`ATHENA_PLUGINS_STATE` et `ATHENA_PLUGINS_DIR` déplacent l'état et les dossiers hors du
dépôt ; le fichier d'état n'est jamais versionné. `ATHENA_DEBUG_DECISIONS=1` rend les
lignes `DEBUG` visibles, mais ce parcours n'en a pas besoin.

### Lire les traces

Les extraits sont rendus **sans l'horodatage**, avec les clés du payload triées comme le
fait le rendu et les identifiants abrégés par `…`. Les lignes du chargeur portent la source
`plugins` ; celles de l'application portent `app`. Le flux est recopié dans
`$ATHENA_LOG_DIR/server.log`.

`PluginState` est la seule trace d'état du chargeur. Un plugin peut émettre ses propres
traces par `context.trace(...)` : `fixture` en émet une, `PluginCreated`. Aucun nom de
trace de plugin réel n'est requis pour comprendre ce document.

## 1. Déposer le plugin synthétique

Créez `$ATHENA_PLUGINS_DIR/fixture/plugin.json` :

```json
{
  "api_version": 1,
  "name": "fixture",
  "version": "0.1.0",
  "description": "Plugin de fixture",
  "platform": "server",
  "entrypoint": "plugin.py"
}
```

Puis `$ATHENA_PLUGINS_DIR/fixture/plugin.py` :

```python
from plugin_api import PluginBinding, ToolBinding, ToolSpec


async def handle(arguments):
    return {"status": "ok", "echo": arguments.get("value", "")}


def create(context):
    context.trace("PluginCreated", {"config": dict(context.config)})
    return PluginBinding(
        name="fixture",
        tools=(
            ToolBinding(
                spec=ToolSpec(
                    name="fixture__echo",
                    description="Écho du plugin",
                    parameters={
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                ),
                handler=handle,
            ),
        ),
        instruction="Utilise fixture__echo pour répéter.",
    )
```

Le manifeste est **validé au chargement** : `api_version` doit valoir `1`, le `name` doit
être identique au nom du dossier, `platform` doit être une plateforme connue, et
l'`entrypoint` doit exister. Le point d'entrée expose `create(context)`, synchrone ou
asynchrone. Le contrat public est `plugin_api` ; un plugin n'importe jamais `server`.

À ce stade, **ne créez pas** le fichier d'état.

## 2. Démarrer sans fichier d'état : découverte et `inactive`

Lancez :

```sh
.venv/bin/python -m server
```

Attendu, avant `started` :

```
INFO [plugins] PluginState {"plugin": "fixture", "state": "inactive"}
INFO [app] started {"state": "Idle"}
```

- Un fichier d'état **absent** signifie « aucun plugin activé », jamais une erreur.
- Le dossier est bien **découvert** et son manifeste lu, mais il n'est **pas importé** :
  aucune ligne `PluginCreated`, aucun Tool dans le registre.
- Aucune activation à chaud : le fichier d'état n'est lu qu'au démarrage.
- Si ni le fichier d'état ni le dossier de plugins n'existent, aucune ligne `PluginState`
  n'est émise : l'instantané porte `"plugins": []`.

Au **premier** démarrage, le serveur tourne sans Compte : le journal porte le Code
d'installation à usage unique. Laissez ce serveur tourner et installez le Compte ; le détail
des écrans et des refus est dans `docs/smoke-test-t5-console.md`.

```sh
CODE=$(sed -n 's/.*InstallationCode.*"code": "\([^"]*\)".*/\1/p' \
  "$ATHENA_LOG_DIR/server.log" | tail -n 1)

curl -i -c /tmp/athena-cookies.txt -X POST http://127.0.0.1:8767/api/setup \
  -H 'Origin: http://127.0.0.1:8767' \
  -H 'Content-Type: application/json' \
  -d "{\"code\":\"$CODE\",\"username\":\"admin\",\"password\":\"un-mot-de-passe-de-12-caracteres\"}"
```

Attendu : `200`, `authenticated: true`, et un cookie `athena_ticket` dans la jar. Aux
démarrages suivants, le Compte existe : passez par `POST /api/login` si la jar a expiré.

Ouvrez la Console (`http://127.0.0.1:8767/`) : le panneau **Plugins** affiche `fixture`
avec le badge **Inactif** et « Aucun Tool ». L'instantané correspondant est :

```json
"plugins": [
  { "name": "fixture", "state": "inactive", "tools": [] }
]
```

Arrêtez le serveur (`Ctrl-C`).

## 3. Activer le plugin par `plugins.json`

Créez `$ATHENA_PLUGINS_STATE` :

```json
{
  "enabled": ["fixture"],
  "config": {}
}
```

Relancez `.venv/bin/python -m server`. Attendu, dans cet ordre :

```
INFO [plugins] PluginCreated {"config": {}, "plugin": "fixture"}
INFO [plugins] PluginState {"plugin": "fixture", "state": "activated", "tools": 1}
INFO [app] started {"state": "Idle"}
```

- L'activation est une **liste explicite** : un dossier présent mais non listé n'est jamais
  importé (étape 2). Seuls les plugins listés le sont, dans l'ordre du fichier d'état.
- `PluginCreated` vient du plugin lui-même (`context.trace`) ; `PluginState` vient du
  chargeur et porte le nombre de Tools du binding.
- Les Tools sont publiés **tels quels** : `fixture__echo`, sa description et son schéma,
  sans préfixe ajouté par Athena.
- L'**Instruction de plugin** est composée dans le prompt système à l'ouverture de chaque
  Session ; ce parcours n'ouvre pas de Session Live.

L'instantané de la Console montre le même état, trié par nom :

```json
"plugins": [
  { "name": "fixture", "state": "activated", "tools": ["fixture__echo"] }
]
```

Le panneau **Plugins** affiche `fixture`, le badge **Activé** et `fixture__echo`.

Arrêtez le serveur.

## 4. Un plugin listé mais absent du disque est `missing`

Avec `fixture` toujours listé dans le fichier d'état, déplacez son dossier hors de
`$ATHENA_PLUGINS_DIR` :

```sh
mv "$ATHENA_PLUGINS_DIR/fixture" "$SMOKE/fixture-hors-ligne"
.venv/bin/python -m server
```

Attendu :

```
INFO [plugins] PluginState {"plugin": "fixture", "state": "missing"}
INFO [app] started {"state": "Idle"}
```

- Le serveur démarre normalement, sans les Tools du plugin.
- Le chargeur **ne réécrit jamais** le fichier d'état : la ligne de configuration est
  conservée pour une réinstallation. Le plugin n'entre pas dans la relance de fond.
- L'instantané porte
  `{ "name": "fixture", "state": "missing", "tools": [] }`.

Arrêtez le serveur et restaurez le dossier :

```sh
mv "$SMOKE/fixture-hors-ligne" "$ATHENA_PLUGINS_DIR/fixture"
```

## 5. Échec fatal, puis panne réparable

Le chargeur distingue deux familles d'échec. Seule `PluginUnavailable` est **réparable** ;
tout le reste est un **défaut permanent**.

### 5.1 Défaut permanent : le démarrage refuse

Remplacez `api_version` par `2` dans `plugin.json`, puis lancez le serveur :

```sh
.venv/bin/python -m server
```

Attendu :

```
Erreur de configuration : plugin 'fixture': unsupported api_version 2 (expected 1)
```

et le code de sortie **2** (`echo $?`). Aucune trace `[app] started` : le chargement des
plugins précède le transport et le démarrage des surfaces. Le message nomme le plugin et
le champ fautif.

Sont également fatals, avec le même refus : un manifeste illisible ou incomplet, un
`api_version` inconnu, un `entrypoint` absent, une configuration ou un secret requis
manquant, un `create()` qui lève autre chose que `PluginUnavailable`, un binding dont le
`name` diffère du dossier, et une collision de Tool (étape 6). Le plugin fautif ne trace
**aucun** `PluginState` : un défaut fatal n'est jamais présenté comme une panne passagère.

Restaurez `api_version` à `1` avant la suite.

### 5.2 Panne réparable : démarrage dégradé et relance

Remplacez `plugin.py` par cette variante, qui échoue à la **première** création puis
réussit à la suivante :

```python
from plugin_api import PluginBinding, PluginUnavailable, ToolBinding, ToolSpec

attempts = 0


async def handle(arguments):
    return {"status": "ok"}


def create(context):
    global attempts
    attempts += 1
    if attempts == 1:
        raise PluginUnavailable("fixture injoignable")
    return PluginBinding(
        name="fixture",
        tools=(
            ToolBinding(
                spec=ToolSpec(name="fixture__echo", description="Écho", parameters={}),
                handler=handle,
            ),
        ),
    )
```

Lancez le serveur. Attendu au démarrage :

```
INFO [plugins] PluginState {"plugin": "fixture", "reason": "PluginUnavailable: fixture injoignable", "state": "failed"}
INFO [app] started {"state": "Idle"}
```

Le serveur est bien `started`, sans les Tools du plugin : la panne d'une plateforme ne rend
pas Athena muette. Puis, au plus tard **5 s** après le démarrage, la relance de fond
réessaie et réussit :

```
INFO [plugins] PluginState {"plugin": "fixture", "retry": true, "state": "activated", "tools": 1}
```

- La raison d'un échec porte le type et le message de l'exception, jamais une valeur de
  secret : les valeurs connues sont remplacées par `***`.
- Chaque tentative infructueuse réémet `PluginState` avec `"retry": true` et
  `"state": "failed"`, et le plugin reste en attente. Le serveur ne redémarre pas.
- Un défaut **structurel** révélé par la relance (par exemple un `create()` qui lève
  `TypeError`, ou une collision de Tool) est tracé `failed` au niveau `error`, puis
  **oublié** : il n'est jamais retenté indéfiniment.
- Les Tools réparés arrivent à la **Connexion Live suivante** : les déclarations d'une
  Session ouverte sont figées à son ouverture.

L'instantané de la Console reflète l'état courant : `failed` puis `activated`, avec
`"tools": []` puis `["fixture__echo"]`.

Arrêtez le serveur et restaurez le `plugin.py` de l'étape 1.

## 6. Collision de nom de Tool : le chargement refuse

La collision est un défaut **permanent**, jamais une panne réparable. Créez un second
plugin synthétique `duplicate` qui déclare le même nom de Tool que `fixture`.

`$ATHENA_PLUGINS_DIR/duplicate/plugin.json` :

```json
{
  "api_version": 1,
  "name": "duplicate",
  "version": "0.1.0",
  "description": "Second plugin de fixture",
  "platform": "server",
  "entrypoint": "plugin.py"
}
```

`$ATHENA_PLUGINS_DIR/duplicate/plugin.py` :

```python
from plugin_api import PluginBinding, ToolBinding, ToolSpec


async def handle(arguments):
    return {"status": "ok"}


def create(context):
    return PluginBinding(
        name="duplicate",
        tools=(
            ToolBinding(
                spec=ToolSpec(
                    name="fixture__echo",
                    description="Le même nom que fixture",
                    parameters={},
                ),
                handler=handle,
            ),
        ),
    )
```

Activez les deux plugins :

```json
{
  "enabled": ["fixture", "duplicate"],
  "config": {}
}
```

Lancez le serveur :

```sh
.venv/bin/python -m server
```

Attendu :

```
Erreur de configuration : plugin 'duplicate': tool 'fixture__echo' is already registered by 'fixture'
```

et le code de sortie **2**, sans `started`. Le registre n'enregistre **jamais à moitié** :
le plugin refusé ne laisse aucun de ses Tools derrière lui.

Une collision découverte **à la relance** (le registre vit déjà) est refusée de la même
façon, tracée `failed` au niveau `error`, et le plugin est retiré de la relance. Un Tool du
plugin refusé n'entre jamais dans le registre.

Arrêtez le serveur, supprimez `$ATHENA_PLUGINS_DIR/duplicate` et remettez
`"enabled": ["fixture"]`.

## 7. Lire l'instantané de la Console

Le panneau **Plugins** et `GET /api/status` lisent le **même** registre. Chaque entrée
porte le nom, l'État de service et les Tools attribués au propriétaire, triés par nom :

```json
"plugins": [
  { "name": "fixture", "state": "activated", "tools": ["fixture__echo"] }
]
```

Les six états sont rendus par le front :

| État brut | Badge de la Console | Sens |
| --- | --- | --- |
| `activated` | **Activé** | importé, Tools publiés |
| `failed` | **En échec** | panne réparable en attente, ou défaut tracé puis oublié |
| `missing` | **Manquant** | listé, dossier absent ; ligne d'état conservée |
| `inactive` | **Inactif** | dossier présent au manifeste sain, absent de la liste d'activation |
| `unsupported` | **Non supporté** | plateforme autre que `server`, donc jamais chargé |
| `invalid` | **Invalide** | manifeste absent, illisible ou incohérent, raison dans la trace |

Pour lire l'API avec le Ticket Console :

```sh
curl -s -b /tmp/athena-cookies.txt http://127.0.0.1:8767/api/status \
  | .venv/bin/python -c 'import json, sys; print(json.dumps(json.load(sys.stdin)["plugins"], indent=2, ensure_ascii=False))'
```

Sans Ticket, la route répond `401` JSON. L'instantané ne porte **aucun secret** : il ne
montre ni configuration de plugin, ni valeur de secret, ni contenu d'appel.

## Limites connues

- **Pas d'activation à chaud** : le fichier d'état n'est lu qu'au démarrage, et les
  déclarations d'une Connexion Live sont figées à son ouverture.
- **Ordre déterministe** : plugins activés dans l'ordre du fichier d'état, puis dossiers
  présents non listés par ordre de nom. L'instantané, lui, trie par nom.
- **Seule `PluginUnavailable` est réparable.** Un manifeste, une configuration, un secret,
  un import, un `create()` qui lève autre chose, ou une collision sont permanents.
- **Un plugin listé et absent** reste `missing` : le chargeur ne modifie jamais le fichier
  d'état.
- **Les secrets ne sont jamais tracés** : ni dans les payloads, ni dans les raisons
  d'échec, ni dans un message de démarrage refusé. Les valeurs connues sont expurgées en
  `***`.
- **Un plugin est amovible** : supprimer son dossier ne doit laisser aucune référence dure
  ailleurs. La convention est dans `plugins/README.md` et l'ADR-0023.

## Vérification automatisée

```sh
make test PYTHON=.venv/bin/python
```

Les comportements de ce document sont couverts par :

- `server/tests/test_plugins_loader.py` — découverte et `inactive` / `unsupported` /
  `invalid` sans import,
  activation et Tools, `missing`, état et dossier absents, manifeste invalide,
  `api_version`, `entrypoint`, configuration et secret requis, `create()` fatal, collision,
  panne `PluginUnavailable` puis relance, relance toujours en échec, défaut structurel
  découvert à la relance, expurgation des secrets, ordre déterministe des états ;
- `server/tests/test_plugins.py` — registre agrégé, propriétaire de chaque Tool, composition
  de l'instruction de plugin ;
- `server/tests/test_plugins_instruction.py` — instruction dans les Sessions ordinaires et
  les Annonces, taille (jamais le texte) dans la trace `Activation` ;
- `server/tests/test_plugin_contract.py` — `plugin_api` importable sans le serveur, aucun
  plugin n'importe un interne du serveur, contrat sans I/O ;
- `server/tests/test_server_main.py` — code de sortie 2, plugin et champ nommés, aucune
  trace `started`, secret jamais exposé ;
- `server/tests/test_app_integration.py` — chargement avant `started`, Tool du plugin
  déclaré, appelé et répondu, panne puis relance sans redémarrage, plugin listé sans
  dossier ;
- `server/tests/test_app_root.py` — instantané réel de `build_app` : `fixture` activé,
  `fixture__echo` listé, aucun secret dans la configuration exposée ;
- `web/src/status.test.ts` — libellés français des quatre états de Plugin.

## Résultat du run

**Non rejoué à la main sur cette tranche.** Le parcours ne dépend d'aucun service externe
et ne met en scène qu'un plugin synthétique ; les extraits décrivent le comportement
implémenté et sont couverts par les tests ci-dessus. La suite complète les rejoue avec le
reste du dépôt.
