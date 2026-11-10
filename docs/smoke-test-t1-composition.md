# Smoke test — T1 composition (`python -m server` + `python -m client`)

Vérification manuelle de bout en bout de la racine de composition (#15) : une seule
boucle asyncio, ordre de démarrage explicite, arrêt propre sur `Ctrl-C`.

## Prérequis

- Un `GEMINI_API_KEY` valide.
- Le protocole généré : `python protocol/proto/generate.py` (ou `make generate`).
- Un micro et un haut-parleur accessibles au client (`pyaudio`).
- Un **Compte** installé et un **Jeton d'accès** de nature `client` (ADR-0017) : le premier
  démarrage décrit l'installation, et le Client refuse de se connecter sans
  `ATHENA_ACCESS_TOKEN`.

Créez un fichier `.env` à la racine du dépôt (lu par le serveur) :

```dotenv
GEMINI_API_KEY=votre-cle
# WS_HOST=0.0.0.0
# WS_PORT=8765
# LOG_LEVEL=INFO
# ATHENA_DEBUG_DECISIONS=0
# ATHENA_LOG_DIR=logs
```

Le serveur refuse de démarrer si `GEMINI_API_KEY` manque : il affiche alors
`Erreur de configuration` et sort avec le code 2.

## 1. Démarrer le serveur et installer l'instance

```sh
python -m server
```

Le flux d'observabilité (ADR-0004) est rendu sur `stderr` et recopié dans
`logs/server.log` (rotation par taille : `server.log.1`…`server.log.3`). L'ordre de
démarrage est explicite : configuration → trace → transport → Gemini → horloge →
Coordinator, puis `started` apparaît dans la chronologie.

Au premier démarrage, sans Compte, la trace `[auth] InstallationCode {"code": "…"}`
apparaît dans le journal. Ouvrez `http://127.0.0.1:8767/` : la Console affiche l'écran
d'installation. Saisissez ce code, un nom d'utilisateur et un mot de passe (12 caractères
minimum), puis créez un **Jeton d'accès** de nature `client` dans le panneau Jetons. Copiez
son secret (`ath_…`) : il n'est affiché qu'une fois. L'écran d'installation disparaît
définitivement dès que le Compte existe.

## 2. Démarrer le client et parler

Dans un second terminal :

```sh
export ATHENA_ACCESS_TOKEN=ath_…   # Jeton de nature client
python -m client
```

Sans cette variable, le Client refuse de démarrer et nomme `ATHENA_ACCESS_TOKEN` ; avec un
Jeton absent, révoqué ou de nature `source`, le serveur refuse le handshake par `401` et le
Client affiche un message explicite.

La **Veille** est active par défaut. Le chargement du modèle précède l'ouverture du micro
et apparaît dans le terminal du client :

```text
Modèle de Wake word chargé (modèle=…/client/models/athena.onnx, seuil=0.50, VAD=désactivé, pré-roll=300 ms)
```

Dites **« Athena »** (prononciation française, « a-té-na »), puis la commande, sans attendre :
la Session démarre à la détection, le pré-roll des 300 ms précédentes est transmis, puis
l'audio live. Le log de détection porte le score :

```text
Wake word détecté (score=0.9…)
Session active
```

Aucun audio n'est envoyé au serveur avant le Wake word. Une détection hors connexion est
ignorée et loguée, jamais rejouée à la reconnexion :

```text
Wake word détecté hors connexion, ignoré (score=…)
```

Le **push-to-talk reste disponible Veille active** : un **Entrée** démarre une Session, et
un nouvel **Entrée** pendant la Session déclenche un barge-in. Pour désactiver la Veille et
n'utiliser que le clavier :

```sh
ATHENA_WAKE_WORD=0 python -m client
```

Le Client logue alors `Veille désactivée (ATHENA_WAKE_WORD=0)` et ne charge aucun modèle.

La Veille est suspendue en Session et tant qu'Athena joue de l'audio (playback non vide,
dernier chunk en cours d'écriture compris) ; elle se réarme après la fin de Session, le
playback vidé et un cooldown de 2 s (`Veille en cooldown pendant 2.0 s`). L'inférence reste
locale et hors ligne, mais hors connexion une détection est seulement loguée, jamais
déclenchée ni rejouée. Si aucun `SessionStarted` ni audio serveur n'arrive dans les 10 s
après un déclenchement, le Client revient en `LISTENING` avec un `WARNING`
(`Session fantôme : aucune confirmation serveur…`) ; ce garde-fou vaut aussi pour le
push-to-talk.

### Variables de la Veille

Toutes sont lues au démarrage ; le modèle et les features sont versionnés, rien n'est
téléchargé.

| Variable | Défaut | Effet |
| --- | --- | --- |
| `ATHENA_WAKE_WORD` | `1` | `0` coupe la Veille et laisse le push-to-talk |
| `ATHENA_WAKE_WORD_MODEL` | `client/models/athena.onnx` | classifieur openWakeWord |
| `ATHENA_WAKE_WORD_THRESHOLD` | `0.5` | seuil ; surchargé par le `threshold` du `.json` posé à côté du modèle |
| `ATHENA_WAKE_WORD_COOLDOWN_SECONDS` | `2.0` | délai de réarmement après une Session |
| `ATHENA_WAKE_WORD_VAD` | `0` | `1` exige que Silero VAD entende de la parole |
| `ATHENA_WAKE_WORD_VAD_MODEL` | `client/models/silero_vad.onnx` | modèle du gate VAD |
| `ATHENA_WAKE_WORD_SESSION_START_TIMEOUT_SECONDS` | `10.0` | borne d'une Session fantôme |
| `ATHENA_WAKE_WORD_PRE_ROLL_MS` | `300` | audio conservé avant la détection |

Pour observer les scores par trame en plus des détections, lancez le Client avec
`LOG_LEVEL=DEBUG` : `Score de Wake word : … (seuil=…)` apparaît alors, throttlé.

### Pannes attendues

- **Modèle ou dépendance absent** — si la Veille est active et qu'`athena.onnx`, un modèle
  de features, le VAD demandé, `openwakeword` ou `onnxruntime` manque, le Client refuse de
  démarrer : `Veille indisponible : …` sur `stderr`, code de sortie 2. Le message nomme le
  chemin ou la dépendance. Aucun micro n'est ouvert.
- **Inférence cassée en cours de route** — une exception du détecteur produit un `ERROR`
  (`Échec de l'inférence du Wake word : Veille désactivée pour le reste du processus,
  push-to-talk disponible`) ; la Veille ne se réarme pas dans ce processus, mais le
  push-to-talk continue de fonctionner.
- **Détection hors connexion** — ignorée et loguée en `WARNING`, non mise en file : elle ne
  crée pas de Session fantôme à la reconnexion.
- **Veille désactivée** — `ATHENA_WAKE_WORD=0` : aucun modèle chargé, aucun coût, le
  push-to-talk reste le seul chemin.

## 3. Validation humaine restant à faire

Le dépôt contient un test d'intégration automatisé qui charge le **vrai** classifieur et les
**vrais** modèles de features, puis vérifie une détection positive sur
`client/tests/data/athena.wav` — un WAV **synthétique Piper** (`fr_FR-siwis-medium`), pas un
enregistrement humain. Ce test ne dépend d'aucun micro.

Il ne remplace pas, et ce smoke test ne prétend pas avoir exécuté :

- **la validation au micro réel** : dire « Athena » avec le serveur et le Client lancés,
  vérifier qu'une Session réelle démarre et que la commande suivante est transmise sans
  perdre sa première syllabe ;
- **la campagne de faux positifs et de faux négatifs** sur plusieurs jours, dans les
  conditions d'usage (bruit, distance, autres voix), avec réglage du seuil,
  `ATHENA_WAKE_WORD_THRESHOLD`, du VAD et du cooldown.

Tant que cette campagne n'a pas été menée par l'utilisateur, `0.5` reste la valeur de
départ, pas un réglage validé.

## 4. Lire la chronologie

Dans le terminal du serveur, la chronologie doit couvrir :

- **Connexions** : `transport` `client_registered` / `client_disconnected` ;
- **Transitions de Session** : `core` `SessionRequested`, `LlmOpened`, `SessionClosed`,
  avec `from_state` / `to_state` ;
- **Appels Gemini** : `gemini` `connected` / `disconnected`, audio et transcriptions.

Avec `ATHENA_DEBUG_DECISIONS=1`, les décisions `debug` (par ex. `ClientAudio`) sont
affichées en plus ; aucun événement n'est retiré du flux.

## 5. Arrêter proprement

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
le démarrage réel de `python -m server` et l'arrêt sur SIGINT. Côté Client, ils couvrent
aussi le port `WakeWordDetector`, le seuil, le VAD et le pré-roll avec des factices, ainsi
qu'un test d'intégration positif sur le WAV synthétique `client/tests/data/athena.wav`
(`skipif` si les modèles ONNX ou la fixture manquent).
