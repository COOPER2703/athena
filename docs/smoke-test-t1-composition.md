# Smoke test — T1 composition (`python -m server` + `python -m client`)

Vérification manuelle de bout en bout de la racine de composition (#15) : une seule
boucle asyncio, ordre de démarrage explicite, arrêt propre sur `Ctrl-C`.

## Prérequis

- Un `GEMINI_API_KEY` valide.
- Le protocole généré : `python protocol/proto/generate.py` (ou `make generate`).
- Un micro et un haut-parleur accessibles au client (`pyaudio`).

Créez un fichier `.env` à la racine du dépôt (lu par le serveur) :

```dotenv
GEMINI_API_KEY=votre-cle
# WS_HOST=0.0.0.0
# WS_PORT=8765
# LOG_LEVEL=INFO
# ATHENA_DEBUG_DECISIONS=0
```

Le serveur refuse de démarrer si `GEMINI_API_KEY` manque : il affiche alors
`Erreur de configuration` et sort avec le code 2.

## 1. Démarrer le serveur

```sh
python -m server
```

Le flux d'observabilité (ADR-0004) est rendu sur `stderr`. L'ordre de démarrage est
explicite : configuration → trace → transport → Gemini → horloge → Coordinator, puis
`started` apparaît dans la chronologie.

## 2. Démarrer le client et parler

Dans un second terminal :

```sh
python -m client
```

Appuyez sur **Entrée** pour demander une Session, puis parlez : la réponse audio est
jouée par le haut-parleur. Un nouvel **Entrée** pendant la Session déclenche un barge-in.

## 3. Lire la chronologie

Dans le terminal du serveur, la chronologie doit couvrir :

- **Connexions** : `transport` `client_registered` / `client_disconnected` ;
- **Transitions de Session** : `core` `SessionRequested`, `LlmOpened`, `SessionClosed`,
  avec `from_state` / `to_state` ;
- **Appels Gemini** : `gemini` `connected` / `disconnected`, audio et transcriptions.

Avec `ATHENA_DEBUG_DECISIONS=1`, les décisions `debug` (par ex. `ClientAudio`) sont
affichées en plus ; aucun événement n'est retiré du flux.

## 4. Arrêter proprement

Dans le terminal du serveur, `Ctrl-C` (SIGINT ou SIGTERM) :

1. dépose un `ShutdownRequested` dans le flux ;
2. le Coordinator déroule son teardown avec le motif `SHUTDOWN` ;
3. les Connexions Live et clients sont fermées ;
4. les tâches d'arrière-plan (pompes d'événements) sont annulées sans tâche orpheline ;
5. `stopped` clôt la chronologie et le processus sort avec le code 0.

## Vérification automatisée

```sh
python -m pytest protocol/tests server/tests client/tests -q
```

Les tests couvrent l'horloge d'inactivité, le fan-in des Événements, le routage des
Commandes, l'arrêt sur `ShutdownRequested` (motif `SHUTDOWN`, aucune tâche orpheline),
le démarrage réel de `python -m server` et l'arrêt sur SIGINT.
