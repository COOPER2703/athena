# Smoke test — T4 plugins et Home Assistant (`python -m server` + `python -m client`)

Vérification manuelle de bout en bout du **système de plugins serveur** et de son premier
plugin, `homeassistant` (spec #47, ADR-0014 et ADR-0015) : au démarrage, Athena découvre les
**Tools Assist** exposés par Home Assistant, compose l'**Instruction de plugin** dans ses
Sessions, puis pilote et interroge la maison à la voix — sans que Gemini ne voie jamais Home
Assistant. Voir `docs/smoke-test-t3-evenements.md` pour les Événements,
`docs/smoke-test-t2-memoire.md` pour la mémoire et `docs/smoke-test-t1-composition.md` pour la
boucle serveur/client d'origine.

## Prérequis

Toutes les commandes se lancent **depuis la racine du dépôt** (le python de la venv locale
s'écrit donc `.venv/bin/python`).

- Un `GEMINI_API_KEY` valide (modèle Live et Consolidation).
- Le protocole généré : `python protocol/proto/generate.py` (ou `make generate`).
- Un micro et un haut-parleur accessibles au client (`pyaudio`).
- Les dépendances à jour, dont le client MCP : `uv sync`.
- **Un Home Assistant réel**, joignable depuis la machine qui lance Athena, avec :
  - l'intégration **Model Context Protocol Server** installée ;
  - un **jeton d'accès longue durée** ;
  - les entités et scripts de test **exposés à Assist** (voir étape 1).
- La machine qui lance Athena doit résoudre l'URL de Home Assistant. `homeassistant.local`
  ne résout pas partout (mDNS) : utilisez l'adresse IP de l'instance si besoin.

### Lire les traces

Les extraits ci-dessous sont rendus **sans l'horodatage**, avec les clés du payload triées
comme le fait le rendu (`render` trie les clés) et les identifiants abrégés par `…`.
`N` désigne un nombre qui dépend de l'instance Home Assistant (Tools découverts, caractères
de l'instruction) : ce n'est **pas** une valeur à retrouver à l'identique. Les lignes
`DEBUG` n'apparaissent qu'avec `ATHENA_DEBUG_DECISIONS=1`.

## 1. Préparer Home Assistant

1. **Installer l'intégration.** Paramètres → Appareils et services → *Ajouter une
   intégration* → **Model Context Protocol Server**. C'est elle qui sert le serveur MCP
   natif de Home Assistant. Si elle n'est pas proposée, mettez Home Assistant à jour.
2. **Endpoint Assist.** Athena se connecte à l'endpoint **`/api/mcp/assist`** (Streamable
   HTTP stateless) : elle **ajoute ce chemin** à l'URL de l'instance configurée. Dans
   `plugins.json`, on ne met donc que `http://homeassistant.local:8123`, jamais
   `/api/mcp` ni `/api/mcp/assist`. Cet endpoint ne demande pas de compte administrateur et
   son périmètre est exactement l'**Exposition Assist**.
3. **Créer un jeton d'accès longue durée.** Profil (en bas à gauche) → *Sécurité* → *Jetons
   d'accès longue durée* → *Créer un jeton*. Copiez-le une seule fois : il part en
   `Authorization: Bearer` à chaque requête et n'est **jamais** écrit par Athena sur disque.
4. **Exposer les entités à Assist.** Paramètres → *Assistants vocaux* → onglet **Exposer**.
   Exposez au minimum :
   - une lumière dont le nom parlé est sans ambiguïté (par ex. `light.salon`) ;
   - un capteur d'ouverture, par ex. la porte du garage (`cover.garage` ou
     `binary_sensor.porte_garage`) ;
   - un script de test (par ex. `script.cinema`) qui produit un effet visible (lumières,
     scène).
   **Seules les entités exposées sont atteignables** : une entité non exposée est invisible
   pour Gemini. C'est le périmètre de sécurité du plugin — il n'y a pas d'allow-list côté
   Athena.
5. **Noter les noms.** Home Assistant résout les noms de pièces et les alias dans **sa**
   langue configurée : dites les noms tels qu'ils existent dans HA. Notez aussi l'`entity_id`
   du script : HA l'expose comme `script__<object_id>` (par ex. `script.cinema` →
   `script__cinema`).

## 2. Créer `plugins.json` et renseigner `.env`

Le dossier du plugin (`plugins/homeassistant/`, avec son `plugin.json` et son `plugin.py`)
est **déjà dans le dépôt**. Il ne reste qu'à l'activer et à fournir son URL et son jeton.

Créez `plugins.json` **à la racine du dépôt** (fichier non versionné, à ne pas commiter) :

```json
{
  "enabled": ["homeassistant"],
  "config": {
    "homeassistant": { "url": "http://homeassistant.local:8123" }
  }
}
```

L'activation est une **liste explicite** : un dossier présent mais non listé n'est jamais
importé. Pas d'activation à chaud — le fichier est lu au démarrage, et la liste des Tools est
de toute façon déclarée à l'ouverture de chaque Connexion Live.

Créez ou complétez `.env` à la racine (lu par le serveur, et ignoré par git) :

```dotenv
GEMINI_API_KEY=votre-cle
ATHENA_USER_ID=owner
# Jeton longue durée Home Assistant. Athena le lit depuis .env comme le reste
# de la configuration ; une variable de processus du même nom l'écrase.
ATHENA_PLUGIN_HOMEASSISTANT_TOKEN=votre-jeton-longue-duree
# Verbeux : indispensable pour voir ToolCall, ToolResult et les annotations
ATHENA_DEBUG_DECISIONS=1
# Emplacements des plugins (défauts : plugins/ et plugins.json)
# ATHENA_PLUGINS_DIR=plugins
# ATHENA_PLUGINS_STATE=plugins.json
# ATHENA_LOG_DIR=logs
# Webhook (actif par défaut) : utile au scénario 9
# ATHENA_WEBHOOK_ENABLED=1
# ATHENA_WEBHOOK_PORT=8766
```

Le nom de la variable de secret est dérivé du manifeste :
`ATHENA_PLUGIN_<PLUGIN>_<SECRET>` → `ATHENA_PLUGIN_HOMEASSISTANT_TOKEN`. Un jeton valide ne
doit jamais être collé dans `plugins.json`, dans un test ou dans un rapport.

## 3. Démarrer le serveur

```sh
python -m server
```

Les plugins sont chargés **avant** le transport et avant la trace `started` : une erreur de
configuration d'un plugin activé empêche le serveur de servir quoi que ce soit. La découverte
Home Assistant (Tools puis instruction) tient dans un budget de 5 s.

Attendu, dans cet ordre (source `plugins`) :

```
INFO     [plugins] ToolsDiscovered {"count": N, "plugin": "homeassistant"}
DEBUG    [plugins] ToolAnnotations {"destructive": false, "idempotent": true, "open_world": true, "plugin": "homeassistant", "read_only": false, "tool": "intent__HassTurnOn"}
INFO     [plugins] InstructionLoaded {"chars": N, "name": "Device control", "plugin": "homeassistant"}
INFO     [plugins] PluginReady {"instruction_chars": N, "plugin": "homeassistant", "tools": N}
INFO     [plugins] PluginState {"plugin": "homeassistant", "state": "activated", "tools": N}
...
INFO     [app] started {"state": "Idle"}
```

- `ToolAnnotations` est émise une fois par Tool (niveau `debug`) : c'est là que vous voyez
  les **vrais noms** publiés par votre version de Home Assistant. Les valeurs des
  annotations (`read_only`, `destructive`, `idempotent`, `open_world`) sont celles de HA et
  peuvent différer de l'exemple ; elles sont tracées mais **inutilisées** en v1 (aucune
  confirmation des Tools destructifs).
- `InstructionLoaded` nomme le prompt Assist découvert par `prompts/list` (`Device control`
  sur les versions actuelles de HA). Si HA le renomme, le plugin retombe sur le nom attendu,
  ou trace `InstructionUnavailable` et sert ses Tools **sans** instruction : l'absence
  d'instruction n'est jamais bloquante.
- Le **texte** de l'instruction n'apparaît ni dans le flux ni dans `logs/server.log` : seuls
  `chars` / `instruction_chars` sont tracés. Vérifiez-le : aucune ligne ne doit contenir la
  phrase d'usage du prompt Assist.
- `N` n'est pas un contrat : le nombre de Tools dépend de la version de HA et de ce que vous
  avez exposé.

## 4. Première Session : l'Instruction de plugin entre dans le prompt

Dans un second terminal :

```sh
python -m client
```

Appuyez sur **Entrée** pour ouvrir une Session, puis parlez. À l'ouverture, le noyau compose
le prompt système, l'instruction du plugin et le contexte mémoire ; la trace `Activation`
donne **la taille** de l'instruction, jamais son texte :

```
INFO [core] Activation {"connection_id": "s1", "context_chars": 0, "has_context": false, "has_plugin_instruction": true, "plugin_instruction_chars": N, "user_id": "owner"}
```

`context_chars` peut être non nul si la mémoire contient déjà un profil épinglé. L'instruction
est **figée à l'ouverture** de la Session : c'est aussi vrai pour une Annonce proactive
(ADR-0013), qui reçoit la même instruction et les mêmes faits, mais **aucun Tool** (`#81`).

## 5. « Allume la lumière du salon »

Dites : « **Allume la lumière du salon** ».

Attendu à l'oral : Athena confirme (par ex. « C'est allumé ») et la lumière s'allume
réellement. Dans le terminal du serveur (avec `ATHENA_DEBUG_DECISIONS=1`) :

```
DEBUG [core] ToolCall {"client_id": "…", "connection_id": "s1", "from_state": "SessionVisible", "name": "intent__HassTurnOn", "plugin": "homeassistant", "session_id": "", "to_state": "SessionVisible", "tool_id": "…"}
INFO  [gemini] tool_result_sent {"connection_id": "s1", "name": "intent__HassTurnOn", "tool_id": "…"}
DEBUG [core] ToolResult {"client_id": "…", "connection_id": "s1", "from_state": "SessionVisible", "name": "intent__HassTurnOn", "plugin": "homeassistant", "session_id": "", "status": "ok", "to_state": "SessionVisible", "tool_id": "…"}
```

- Le nom est celui de HA, **repris tel quel** : sur une version récente, l'intent d'allumage
  est `intent__HassTurnOn`. Les Tools propres au composant HA sont `homeassistant__*`, les
  scripts `script__*` ; une version plus ancienne de HA peut publier des noms sans préfixe.
  Ne déduisez donc pas le nom de ce document : lisez-le dans `ToolAnnotations` (étape 3) ou
  dans `ToolCall`.
- `plugin: "homeassistant"` attribue l'appel à son propriétaire (un Tool du noyau porte
  `plugin: "core"`).
- `from_state` peut être `LiveActive` au tout premier tour ; ensuite la Session est
  `SessionVisible`.
- Un succès ne produit **aucune** ligne `[plugins] ToolFailed`.

## 6. Question d'état

Dites : « **Est-ce que la porte du garage est ouverte ?** »

Attendu à l'oral : une réponse qui correspond à la réalité (ouvrez/fermez la porte pour
vérifier). La trace montre un Tool de **lecture** :

```
DEBUG [core] ToolCall {… "name": "<outil de lecture>", "plugin": "homeassistant" …}
DEBUG [core] ToolResult {… "name": "<outil de lecture>", "plugin": "homeassistant", "status": "ok" …}
```

Le nom dépend de la version de Home Assistant et des Tools exposés : `GetLiveContext`
(`homeassistant__GetLiveContext`, un instantané de contexte) ou `intent__HassGetState`.
**Ce document ne fige aucun des deux** : le nom attendu est celui que HA a déclaré, visible
dans `ToolAnnotations` / `ToolCall`. Le texte de HA (état de la porte) n'est pas dans la
trace : il part dans le `message` du résultat, vers le modèle.

## 7. Script

Exposez un script à Assist (par ex. `script.cinema`, qui baisse les lumières). Dites :
« **Lance le script cinéma** ».

Attendu : la scène s'exécute réellement. Trace :

```
DEBUG [core] ToolCall {… "name": "script__cinema", "plugin": "homeassistant" …}
DEBUG [core] ToolResult {… "name": "script__cinema", "plugin": "homeassistant", "status": "ok" …}
```

Le préfixe `script__` et l'`object_id` viennent de HA (`script.cinema` → `script__cinema`) ;
Athena n'ajoute ni ne retire aucun préfixe. Le nom exact reste celui publié par votre
instance.

## 8. Désactiver le plugin

Arrêtez le serveur (`Ctrl-C`), puis retirez `homeassistant` de la liste d'activation —
gardez la configuration pour pouvoir le réactiver :

```json
{
  "enabled": [],
  "config": {
    "homeassistant": { "url": "http://homeassistant.local:8123" }
  }
}
```

Relancez `python -m server`. Attendu :

```
INFO [plugins] PluginState {"plugin": "homeassistant", "state": "disabled"}
INFO [app] started {"state": "Idle"}
```

- Aucune ligne `ToolsDiscovered`, `InstructionLoaded` ni `PluginReady` : le dossier est
  présent mais **jamais importé**.
- La Session suivante trace `Activation` avec `has_plugin_instruction: false` et
  `plugin_instruction_chars: 0`.
- Demandez « allume la lumière du salon » : Athena répond qu'elle ne peut pas piloter la
  maison, et **aucun** `ToolCall` / `ToolResult` n'apparaît. Tools et instruction
  disparaissent ensemble.
- Le dossier `plugins/homeassistant/` n'a pas été touché. Remettez `["homeassistant"]` et
  redémarrez pour la suite. (Si vous laissez `homeassistant` listé et supprimez le dossier,
  l'état tracé est `missing` et votre ligne de configuration est conservée.)

## 9. Home Assistant éteint au démarrage

Arrêtez Home Assistant (ou pointez `url` vers un hôte injoignable), puis démarrez Athena.
Attendu : un démarrage **dégradé**, pas un refus.

```
WARNING [plugins] DiscoveryFailed {"error": "…", "plugin": "homeassistant"}
INFO    [plugins] PluginState {"plugin": "homeassistant", "reason": "PluginUnavailable: Home Assistant discovery failed: …", "state": "failed"}
INFO    [app] started {"state": "Idle"}
```

La raison contient le type et le message de l'erreur de transport (connexion refusée, nom
non résolu, délai dépassé), jamais le jeton. Le serveur est bien `started`, sans les Tools du
plugin. Pendant la panne, la retente de fond réessaie **toutes les 5 s** et trace à chaque
tentative `DiscoveryFailed` puis :

```
INFO [plugins] PluginState {"plugin": "homeassistant", "reason": "…", "retry": true, "state": "failed"}
```

Prouvez que le reste fonctionne :

1. **Athena parle** : ouvrez une Session et conversez. `Activation` affiche
   `has_plugin_instruction: false`, `plugin_instruction_chars: 0`.
2. **Athena se souvient** : dites « **Souviens-toi que je m'appelle Camille** ». Trace
   attendue (le Tool du noyau, propriétaire `core`) :
   `ToolCall name="save_memory" plugin="core"` puis `ToolResult status="ok"`.
3. **Les Événements passent** : laissez le Client au Repos (pas de Session — attendez la
   fermeture par inactivité, ou relancez `python -m client`) et poussez un fait :

   ```sh
   curl -i -X POST http://127.0.0.1:8766/events \
     -H 'Content-Type: application/json' \
     -d '{"source": "maison", "content": "La porte d entree vient de s ouvrir"}'
   ```

   Attendu : `202 Accepted`, puis `[webhook] event_received`, `[core] EventWindowOpened`, et
   après la fenêtre de 5 s le jugement (`[core] EventJudge`, `[judge] judgment_done`). Le
   verdict peut annoncer ou non : dans les deux cas le Canal d'événements a fonctionné.

Rallumez Home Assistant **sans toucher à Athena**. Dans les 5 s qui suivent (plus la durée de
la découverte) :

```
INFO [plugins] ToolsDiscovered {"count": N, "plugin": "homeassistant"}
INFO [plugins] InstructionLoaded {"chars": N, "name": "Device control", "plugin": "homeassistant"}
INFO [plugins] PluginReady {"instruction_chars": N, "plugin": "homeassistant", "tools": N}
INFO [plugins] PluginState {"plugin": "homeassistant", "retry": true, "state": "activated", "tools": N}
```

Il n'y a **qu'une** ligne `started` : Athena n'a pas redémarré. Les Tools arrivent à la
**Connexion Live suivante** — les déclarations d'une Session ouverte sont figées à son
ouverture. Ouvrez donc une nouvelle Session (Entrée) puis redemandez la lumière : elle
fonctionne. `Registered.server_tools` est lui aussi relu à chaque `Register` (étape 12).

Tant que le plugin est en échec, demander la lumière ne produit aucun `ToolCall` : Athena
répond qu'elle ne peut pas.

## 10. Une commande échouée est annoncée, pas passée sous silence

Provoquez un refus de Home Assistant. Deux moyens :

- demander une entité **non exposée à Assist** (par ex. retirer temporairement l'exposition
  de la lumière du salon, ou demander une pièce qui n'est pas exposée) ;
- lancer un script exposé dont l'action échoue.

Si le modèle refuse de lui-même sans appeler de Tool (le nom ne lui dit rien), reformulez en
désignant explicitement la pièce : c'est l'**appel qui échoue** qui est testé ici. Pensez à
restaurer l'exposition ensuite.

Attendu à l'oral : Athena dit qu'elle **n'a pas pu** (par ex. « Je n'arrive pas à joindre
cette lumière »), et ne prétend pas que c'est fait. Trace :

```
DEBUG [core] ToolCall {… "name": "intent__HassTurnOn", "plugin": "homeassistant" …}
INFO  [gemini] tool_result_sent {"connection_id": "s1", "name": "intent__HassTurnOn", "tool_id": "…"}
DEBUG [core] ToolResult {… "plugin": "homeassistant", "status": "error" …}
```

Le plugin rend `{"status": "error", "message": …}` avec **le texte de HA** (débarrassé de
l'enveloppe du SDK) : le modèle peut donc expliquer le refus. Le libellé exact dépend de HA.
La Session survit : la question suivante fonctionne et aucune ligne `SessionClosed` n'est
émise.

Nuance utile : `[plugins] ToolFailed` est réservé aux pannes de transport (Home Assistant
injoignable ou délai de 10 s dépassé). Un `isError` renvoyé par HA est un `ToolResult` en
erreur **normal**, sans `ToolFailed`.

## 11. Token absent : le démarrage refuse

Arrêtez le serveur, retirez la ligne `ATHENA_PLUGIN_HOMEASSISTANT_TOKEN` de `.env`, et
assurez-vous qu'elle n'est pas exportée dans le shell (`unset`). Relancez :

```sh
python -m server
```

Attendu :

```
Erreur de configuration : plugin 'homeassistant': required secret ATHENA_PLUGIN_HOMEASSISTANT_TOKEN is missing from the environment
```

et le code de sortie **2** (`echo $?`). Aucune trace `[app] started`, aucun service démarré :
le message nomme le plugin **et** la variable manquante. La valeur du jeton, elle, n'apparaît
ni dans ce message ni dans le flux ni dans `logs/server.log`.

Restaurez la ligne. Seul un secret **manquant**, une configuration requise absente ou un
manifeste invalide est fatal : un **mauvais** jeton est traité comme une plateforme
injoignable (scénario 9), avec démarrage dégradé et retente.

## 12. `Registered.server_tools`

À chaque enregistrement, le serveur envoie d'abord une trame `Registered` dont le champ
`server_tools` liste **tous** les Tools serveur : les deux Tools du noyau (`save_memory`,
`forget_memory`) puis les Tools Assist du plugin. Le schéma du protocole client est
**inchangé** : le champ `repeated string server_tools = 2` existait déjà, et le client
desktop l'ignore volontairement (il ne lit que `client_id`).

Pour l'observer, lancez ceci **avant** `python -m client` (un seul client à la fois, pour ne
pas déplacer la cible des Annonces) :

```sh
.venv/bin/python - <<'PY'
import asyncio

import websockets

from protocol import Register, decode, encode


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        await ws.send(
            encode(Register(client_name="smoke-tools", platform="linux"))
        )
        registered = decode(await ws.recv())
        print("client_id:", registered.client_id)
        for name in registered.server_tools:
            print(name)


asyncio.run(main())
PY
```

Attendu (l'ordre et les noms Home Assistant dépendent de votre instance et de votre
exposition) :

```
client_id: …
save_memory
forget_memory
intent__HassTurnOn
homeassistant__GetLiveContext
script__cinema
```

Si le plugin a échoué au démarrage, seuls `save_memory` et `forget_memory` apparaissent. La
trame est régénérée à chaque `Register` : après une retente réussie, une nouvelle connexion
annonce les Tools du plugin sans redémarrage d'Athena.

## Limites connues

- **Mauvais jeton** : indistinguable d'une plateforme injoignable → démarrage dégradé et
  retente toutes les 5 s. Seul un secret ou une configuration requis **manquant** (ou un
  manifeste invalide) est fatal.
- **Langue** : HA résout les noms de zones et d'alias dans **sa** langue configurée. Athena
  ne transmet ni langue ni `device_id` ; parlez les noms tels qu'ils existent dans HA.
- **Nouveau Tool côté HA** (script, nouvelle exposition) : visible seulement au **redémarrage
  suivant d'Athena**, car la découverte a lieu au démarrage. Un redémarrage de HA, lui, ne
  casse rien : une session MCP neuve est ouverte à chaque appel.
- **Ressources MCP** : `resources/read` et le `context-snapshot` de HA sont hors périmètre.
- **Événements poussés par le plugin** : hors périmètre v1 ; le MCP ne pousse rien.
- **Tools destructifs** : les annotations sont tracées mais inutilisées ; aucune confirmation
  côté Athena. Le seul garde-fou est l'**Exposition Assist**.
- **Coût d'un appel** : une session MCP par appel (le serveur HA est stateless), donc une
  poignée de main `initialize` à chaque Tool. Le délai d'appel d'Athena est de 10 s.
- **Pas d'activation à chaud** : `plugins.json` n'est lu qu'au démarrage.

## Vérification automatisée

```sh
python -m pytest protocol/tests server/tests client/tests -q
```

Les scénarios ci-dessus s'appuient sur :

- `server/tests/test_plugins_homeassistant.py` — le **vrai** plugin contre un serveur MCP HA
  en processus (`server/tests/ha_stub.py`) : découverte, déclarations telles quelles, appels,
  texte `isError`, délais, maison injoignable, redémarrage de HA entre deux appels, jeton
  jamais tracé ;
- `server/tests/test_plugins.py` — agrégation du registre, collision de noms, attribution du
  propriétaire, composition de l'instruction ;
- `server/tests/test_plugins_loader.py` — états `activated` / `disabled` / `missing` /
  `failed`, retente, cas fatals, expurgation des secrets ;
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

**Non exécuté à ce jour.** Ce document a été rédigé dans un environnement sans Home Assistant
réel ni `GEMINI_API_KEY` valide : aucun parcours vocal n'a été joué et aucune trace réelle
n'est reproduite ici. Les extraits décrivent le comportement **implémenté**, couvert par les
tests listés ci-dessus ; les `N` et les noms de Tools restent à relever sur une instance
réelle. Ce document n'est donc **pas** un compte rendu de run.
