# Plugin Home Assistant

Le plugin `homeassistant` branche Athena sur le **serveur MCP natif de Home Assistant**
(ADR local [0001](docs/adr/0001-home-assistant-via-mcp.md)). Au démarrage, il découvre les
**Tools Assist** et le prompt « Device control », puis les expose à Gemini tels quels :
noms, descriptions et schémas viennent de Home Assistant, sans filtrage ni renommage. Une
Session peut ainsi allumer une lumière, lire l'état de la maison ou lancer un script à la
voix, sans que Gemini ne parle directement à Home Assistant.

Athena ne se connecte ni à l'API REST ni au WebSocket de Home Assistant : elle ajoute au
contraire le chemin `/api/mcp/assist` à l'URL configurée. Le plugin n'émet aucun Événement
en v1 ; le MCP ne pousse rien.

## Activation

Le dossier est versionné, mais **rien ne s'exécute tant qu'il n'est pas listé** dans le
fichier d'état local `plugins.json` (non versionné ; `ATHENA_PLUGINS_STATE`, `/data/plugins.json`
dans l'Image). L'activation est lue au démarrage : il n'y a pas d'activation à chaud.

```json
{
  "enabled": ["homeassistant"],
  "config": {
    "homeassistant": { "url": "http://homeassistant.local:8123" }
  }
}
```

L'option `url` est **requise** et ne porte que l'URL de l'instance, sans chemin MCP :
`http://homeassistant.local:8123`, jamais `/api/mcp` ni `/api/mcp/assist`. La machine qui
lance Athena doit résoudre cette URL ; `homeassistant.local` ne résout pas partout (mDNS),
utilisez l'adresse IP si besoin.

## Secret

Le manifeste déclare un secret **requis** : `ATHENA_PLUGIN_HOMEASSISTANT_TOKEN`, qui porte
un **jeton d'accès longue durée** Home Assistant (Profil → Sécurité → Jetons d'accès longue
durée). Il se fournit dans l'environnement ou dans `.env` (une variable de processus du même
nom l'écrase) :

```dotenv
ATHENA_PLUGIN_HOMEASSISTANT_TOKEN=votre-jeton-longue-duree
```

Absent, il fait échouer le démarrage en nommant la variable, avec le code de sortie `2` :
un secret requis n'est jamais toléré. La valeur part en `Authorization: Bearer` à chaque
requête et n'apparaît ni dans une Trace, ni dans le journal, ni dans l'instantané de la
Console.

## Prérequis côté Home Assistant

- l'intégration **Model Context Protocol Server** installée (Paramètres → Appareils et
  services → Ajouter une intégration) ;
- un **jeton d'accès longue durée** créé et copié une seule fois ;
- les entités et scripts de test **exposés à Assist** (Paramètres → Assistants vocaux →
  onglet Exposer). Seules les entités exposées sont atteignables : c'est le périmètre de
  sécurité du plugin, il n'existe aucune allow-list côté Athena ;
- des noms de pièces et d'alias sans ambiguïté : Home Assistant les résout dans **sa**
  langue configurée (aucune langue n'est transmise par le MCP).

## Démarrage, panne et reprise

La découverte (Tools puis instruction) tient dans un budget de **5 s** et l'instruction est
composée dans le prompt des Sessions qui déclarent ces Tools. Un appel de Tool a un budget
de **10 s**. Les Traces de démarrage, de source `plugins`, sont :

```
INFO  [plugins] ToolsDiscovered {"count": N, "plugin": "homeassistant"}
INFO  [plugins] InstructionLoaded {"chars": N, "name": "Device control", "plugin": "homeassistant"}
INFO  [plugins] PluginReady {"instruction_chars": N, "plugin": "homeassistant", "tools": N}
INFO  [plugins] PluginState {"plugin": "homeassistant", "state": "activated", "tools": N}
```

Le texte de l'instruction n'est jamais tracé : seuls `chars` et `instruction_chars` le
sont. Si Home Assistant ne publie pas le prompt attendu, le plugin trace
`InstructionUnavailable` et sert ses Tools **sans** instruction : l'absence n'est jamais
bloquante. Une instruction est figée à l'ouverture d'une Session ; de même, les Tools sont
déclarés à la Connexion Live suivante, pas dans une Session déjà ouverte.

**Démarrage dégradé.** Une découverte injoignable, un jeton refusé ou un délai dépassé ne
sont pas fatals : `create()` lève `PluginUnavailable`, la Console montre le plugin `failed`
et Athena démarre quand même, sans les Tools du plugin. Le serveur retente toutes les **5 s**
et s'active seul dès que Home Assistant répond ; aucun redémarrage d'Athena n'est
nécessaire. Seul un secret **manquant**, une configuration requise absente ou un manifeste
invalide est fatal.

**Panne en cours de Session.** Un appel qui échoue rend `{"status": "error", "message": …}`
au modèle avec le texte de Home Assistant (débarrassé de l'enveloppe du SDK) : la Session
survit et Athena peut expliquer le refus. Un `ToolFailed` est réservé aux pannes de
transport (Home Assistant injoignable ou délai dépassé). Les annotations MCP (`read_only`,
`destructive`, `idempotent`, `open_world`) sont tracées mais **inutilisées** en v1 : aucune
confirmation des Tools destructifs, le garde-fou reste l'Exposition Assist.

Une session MCP neuve est ouverte à **chaque appel** (le serveur HA est stateless) : un
redémarrage de Home Assistant entre deux appels ne casse rien. En revanche, un nouveau Tool
ou une nouvelle exposition côté HA n'apparaît qu'au redémarrage suivant d'Athena, puisque
la découverte a lieu au démarrage.

## Documentation locale

- [Smoke test du plugin](docs/smoke-test.md) — le parcours manuel de bout en bout.
- [ADR local 0001](docs/adr/0001-home-assistant-via-mcp.md) — les décisions du plugin.
- [Glossaire du plugin](GLOSSARY.md) — le vocabulaire de domotique propre à Home Assistant.
- [Tests](tests/test_plugins_homeassistant.py) et [faux serveur MCP](tests/ha_stub.py).
