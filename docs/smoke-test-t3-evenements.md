# Smoke test — T3 événements (`python -m server` + `python -m client` + `curl`)

Vérification manuelle de bout en bout du **Canal d'événements** (spec #35) par l'**accès
Webhook** (issue #44, ADR-0011) : un fait poussé en HTTP traverse le Canal unique, ouvre la
fenêtre de regroupement, est jugé au Repos, et — s'il le mérite — ouvre une **Annonce
proactive** entendue par le Client. Voir `docs/smoke-test-t2-memoire.md` pour la mémoire et
`docs/smoke-test-t1-composition.md` pour la boucle serveur/client d'origine.

## Prérequis

Toutes les commandes se lancent **depuis la racine du dépôt** (le python de la venv locale
s'écrit donc `.venv/bin/python`).

- Un `GEMINI_API_KEY` valide (modèle Live et Consolidation).
- Le protocole généré : `python protocol/proto/generate.py` (ou `make generate`).
- Un micro et un haut-parleur accessibles au client (`pyaudio`).
- Les dépendances à jour (ajoute `aiohttp`) : `uv sync`.
- **Un Client connecté** : sans lui, le noyau juge quand même l'Événement, mais
  `_registered_client_id` est vide et l'Annonce n'a **aucune cible** — personne ne l'entend.

Créez un fichier `.env` à la racine (lu par le serveur) :

```dotenv
GEMINI_API_KEY=votre-cle
ATHENA_USER_ID=owner
# Webhook (ADR-0011) : lié à localhost par défaut, aucun jeton
# ATHENA_WEBHOOK_ENABLED=1
# ATHENA_WEBHOOK_HOST=127.0.0.1
# ATHENA_WEBHOOK_PORT=8766
# Verbeux : ajoute les décisions détaillées au flux (sinon niveau INFO)
# ATHENA_DEBUG_DECISIONS=1
```

## 1. Démarrer le serveur

```sh
python -m server
```

Le flux d'observabilité (ADR-0004) est rendu sur `stderr`. La mémoire est réchauffée
(chargement du modèle d'embeddings), puis `started` apparaît. Le serveur écoute aussi la
surface Webhook sur `http://127.0.0.1:8766` (aucune trace de démarrage dédiée : le `202` de
l'étape 3 la confirme).

## 2. Démarrer le Client et le laisser au Repos

Dans un second terminal :

```sh
python -m client
```

Le Client s'enregistre à la connexion. **Ne pressez pas Entrée** : le noyau doit rester au
**Repos** (aucune Session) pour juger un Événement. (Une pression sur Entrée ouvrirait une
Session ; le fait serait alors mis en File et jugé au retour au Repos.)

## 3. Pousser un fait

```sh
curl -i -X POST http://127.0.0.1:8766/events \
  -H 'Content-Type: application/json' \
  -d '{"source": "maison", "content": "La porte d entree vient de s ouvrir"}'
```

Attendu :

```
HTTP/1.1 202 Accepted
Content-Type: application/json; charset=utf-8

{"status": "accepted"}
```

## 4. Lire le flux de trace

Dans le terminal du serveur, cherchez (l'ordre est celui du Canal unique) :

- `[webhook] event_received` — `source`, `chars` : le fait est entré ;
- `[core] EventQueued` (debug) — mis en File ; `queued` = taille courante ;
- `[core] EventWindowOpened` — **fenêtre paresseuse de 5 s** armée par ce premier fait ;
- après l'échéance : `[core] EventJudge` (`events` = taille du lot) puis
  `[judge] judgment_done` (`announce`) — le verdict vient d'un **modèle texte** ;
- si le verdict annonce (`announce=true`) : une **seule** Connexion Live d'Annonce
  (sans Rappel ni Tools), `[gemini] connected` puis `announcement_sent` ; l'instruction
  est celle de l'Annonce (`announce-prompt.txt`) et le message est celui du juge ;
- si le LLM parle : `[core] LlmTranscription` puis `[core] AnnounceSessionStarted` avec
  `auto=true` — le Client reçoit une **Annonce proactive** et le fait entre au Journal en
  rôle `event` ;
- si le verdict est `announce=false` : **aucune Connexion Live n'est ouverte** et le fait
  tu n'entre pas au Journal (faute fermée si le jugement échoue).

## 5. Cas d'erreur

```sh
# 400 : contenu vide
curl -i -X POST http://127.0.0.1:8766/events -H 'Content-Type: application/json' \
  -d '{"source": "maison", "content": "   "}'

# 415 : mauvais Content-Type
curl -i -X POST http://127.0.0.1:8766/events -H 'Content-Type: text/plain' -d 'hello'

# 413 : contenu au-delà de 4096 caractères
curl -i -X POST http://127.0.0.1:8766/events -H 'Content-Type: application/json' \
  -d "{\"content\": \"$(printf 'x%.0s' {1..4097})\"}"

# 404 : chemin inconnu ; 405 : mauvaise méthode
curl -i -X POST http://127.0.0.1:8766/nope -H 'Content-Type: application/json' -d '{}'
curl -i http://127.0.0.1:8766/events
```

## 6. Rafale et dédup intra-fenêtre

Répétez plusieurs fois la même requête **dans les 5 s** :

```sh
for i in 1 2 3; do
  curl -s -X POST http://127.0.0.1:8766/events -H 'Content-Type: application/json' \
    -d '{"source": "maison", "content": "La porte vient de s ouvrir"}'
done
```

Attendu : un **seul** jugement à l'échéance ; les répétitions exactes `(source, contenu)`
tracées `[core] EventQueueDropped` avec `reason="duplicate"`.

## 7. « L'utilisateur gagne toujours »

Poussez un fait, puis, **pendant la fenêtre des 5 s**, pressez Entrée côté Client : la trace
`[core] EventWindowCanceled` apparaît, le fait **reste en File**, et il est jugé au prochain
Repos (fin de Session).

## Vérification automatisée

```sh
python -m pytest protocol/tests server/tests client/tests -q
```

La couture de l'adaptateur est couverte par `server/tests/test_webhook_adapter.py` (succès,
Source par défaut, champs vides, JSON invalide, Content-Type, taille, 404, 405) ; la config par
`server/tests/test_app_config.py` ; le câblage `App.run → push_event` par
`server/tests/test_app_root.py`.

## Résultat du run

**Date** : 2026-10-17 · **Machine** : poste de dev Linux · **Python** : 3.14 (venv locale
`.venv`, à exécuter depuis la racine du dépôt) · `aiohttp` 3.14.5.

**Ce qui a été exécuté pour de vrai** (chemin non-voix : audio/VAD humain non automatisable) —
serveur réel `WS_PORT=8791 ATHENA_WEBHOOK_PORT=8792`, base mémoire temporaire, `curl` réel :

1. **Démarrage** — `started`, puis écoute WebSocket et surface Webhook (le `202` de l'étape 3
   confirme la liaison — pas de trace de démarrage dédiée, conforme à l'ADR-0011) :

   ```
   INFO [app] started {"state": "Idle"}
   INFO [consolidation] Consolidation {"duplicates": 0, "facts_extracted": 0, "facts_stored": 0}
   ```

2. **`POST /events` nominal** — `202 Accepted` + `{"status":"accepted"}`, le fait entre dans le
   Canal et arme la fenêtre de regroupement :

   ```
   INFO [webhook] event_received {"chars": 33, "source": "maison"}
   INFO [logging] aiohttp.access {"message": "127.0.0.1 [17/Oct/2026:00:40:02 +0200] \"POST /events HTTP/1.1\" 202 187 \"-\" \"curl/8.22.0\""}
   INFO [core] EventWindowOpened {"client_id": "", "events": 1, "from_state": "Idle", "to_state": "Idle"}
   ```

3. **Cas d'erreur** — tous les codes attendus, en conditions réelles :
   `400` (contenu vide), `415` (Content-Type), `404` (chemin `/nope`), `405` (`GET /events`).

4. **Jugement réel au bout des 5 s** — le verdict vient du **modèle texte** ; un fait
   important ouvre alors **une seule** Connexion Live d'Annonce, sans Rappel ni Tools :

   ```
   INFO     [core] EventWindowOpened {"events": 1, ...}
   DEBUG    [judge] judgment_done {"announce": true, "facts": 1}
   INFO     [gemini] connected {"connection_id": "s1", "model": "gemini-3.1-flash-live-preview", "voice": "Zephyr"}
   INFO     [gemini] announcement_sent {"chars": 36, "connection_id": "s1"}
   INFO     [core] EventJudge {"announce": true, "connection_id": "s1", "events": 1, "from_state": "Idle", "to_state": "LiveConnecting"}
   INFO     [core] LlmAudio  {"connection_id": "s1", "from_state": "LiveActive", "to_state": "SessionVisible", ...}
   ```

   Et un fait **banal** ne déclenche **rien** : le jugement texte le tait, aucune Connexion
   Live n'est ouverte et le fait n'entre pas au Journal.

   ```
   INFO     [core] EventWindowOpened {"events": 1, ...}
   DEBUG    [judge] judgment_done {"announce": false, "facts": 1}
   INFO     [core] EventJudged {"announce": false, "events": 1, "from_state": "Idle", "to_state": "Idle"}
   ```

5. **Aucun Client enregistré** — l'Annonce proactive est bien émise, mais sans cible : la
   surface WebSocket ne trouve aucun client. C'est exactement le prérequis de l'étape 2 :

   ```
   WARNING [transport] client_not_found {"client_id": "", "type": "AnnounceSessionStarted"}
   ```

6. **Arrêt propre** (`SIGTERM`) — la Session en vol se referme par le chemin déterministe :

   ```
   INFO [core] Shutdown    {"reason": "SHUTDOWN", "from_state": "SessionVisible", "to_state": "Closing"}
   INFO [core] SessionClosed {"reason": "SHUTDOWN", "from_state": "Closing", "to_state": "Idle"}
   INFO [app] stopped
   ```

**Différé à un humain (non automatisable ici) et pourquoi** :

- **Entendre l'Annonce** : il faut un Client connecté (micro + enceinte + parole). Le run
  ci-dessus prouve le pipeline jugement → Annonce ; l'écoute à l'oreille reste à faire.
- **Fenêtre paresseuse sous rafale / dédup / « l'utilisateur gagne toujours »** (sections 6 et
  7) : le comportement est couvert par les tests du noyau (`test_event_queue_limits.py`,
  `test_event_judgement.py`) ; non rejoué à la main dans ce run.

**Observation** : les requêtes apparaissent aussi dans le flux via `[logging] aiohttp.access`
(l'adaptateur est branché sur le pont de logs existant, comme `websockets`). À surveiller si un
volume d'événements élevé rend le flux bavard.

**Vérification automatisée** :

```
$ PYTHONPATH=. python -m pytest protocol/tests server/tests client/tests -q
481 passed
```
