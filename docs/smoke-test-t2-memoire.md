# Smoke test — T2 mémoire (`python -m server` + `python -m client`)

Vérification manuelle de bout en bout de la mémoire (spec #24) : Activation,
Rappel, Mémorisation explicite et Consolidation, à l'oreille et dans le flux
d'observabilité unique (ADR-0004). Voir `docs/smoke-test-t1-composition.md` pour
la boucle serveur/client d'origine.

## Prérequis

- Un `GEMINI_API_KEY` valide (modèle Live et Consolidation).
- Le protocole généré : `python protocol/proto/generate.py` (ou `make generate`).
- Un micro et un haut-parleur accessibles au client (`pyaudio`).
- Les dépendances mémoire pré-installées : `sentence-transformers`, `sqlite-vec`,
  `numpy` (modèle local `all-MiniLM-L6-v2` en cache).

Créez un fichier `.env` à la racine du dépôt (lu par le serveur) :

```dotenv
GEMINI_API_KEY=votre-cle
ATHENA_USER_ID=owner
# Chemin de la base mémoire (défaut : athena_memory.db dans le cwd)
# ATHENA_MEMORY_DB=athena_memory.db
# Verbeux : ajoute les décisions détaillées au flux (sinon niveau INFO)
# ATHENA_DEBUG_DECISIONS=1
# Créneaux de Consolidation (heure locale, rattrapage au démarrage)
# ATHENA_CONSOLIDATION_HOURS=0,12
```

Le serveur refuse de démarrer si `GEMINI_API_KEY` manque : il affiche
`Erreur de configuration` et sort avec le code 2.

## 1. Démarrer le serveur

```sh
python -m server
```

Le flux d'observabilité (ADR-0004) est rendu sur `stderr`. Au démarrage, la
mémoire est réchauffée (chargement du modèle d'embeddings), puis `started`
apparaît. Le planificateur rattrape immédiatement un créneau de Consolidation
manqué (ADR-0009) : une ligne `[consolidation] Consolidation` peut donc
apparaître dès le démarrage.

## 2. Démarrer le client et tenir une Session

Dans un second terminal :

```sh
python -m client
```

Appuyez sur **Entrée** pour demander une Session, puis parlez. À
`SessionRequested`, le noyau assemble l'instruction de Session
(prompt système + contexte mémoire) : la trace `[core] Activation` apparaît avec
`has_context` et `context_chars`.

## 3. Mémorisation explicite (« souviens-toi que… »)

Dites une phrase d'explicite, par ex. « **Souviens-toi que je m'appelle
Camille** » ou « **Retiens que mon anniversaire est le 3 mars** ». Le modèle doit
appeler le Tool `save_memory`.

Dans le terminal du serveur, avec `ATHENA_DEBUG_DECISIONS=1`, on doit voir le
round-trip du Tool :

- `[core] ToolCall` (debug) — `name="save_memory"`, `tool_id` ;
- `[gemini] tool_result_sent` — le résultat renvoyé au modèle ;
- `[core] ToolResult` (debug) — `status="ok"`, `name="save_memory"`.

Le fait est écrit dans la table `facts` : `kind="profile"` l'épingle (toujours
injecté, Activation), `kind="fact"` le rend rappelable par recherche (Rappel).

## 4. Redémarrer le serveur et vérifier le Rappel

Arrêtez le serveur (`Ctrl-C`), relancez-le, démarrez une **nouvelle** Session et
reparlez. Le fait mémorisé doit être :
- présent dès l'ouverture s'il est épinglé (Activation) ;
- rappelé quand la conversation s'y rapporte (Rappel).

La question qui déclenche le Rappel doit être proche sémantiquement du fait
(similarité cosinus < `ATHENA_RECALL_MAX_DISTANCE`, défaut `0.6`). Une paraphrase
trop éloignée ne déclenche rien.

## 5. Lire le flux de trace mémoire

Dans le terminal du serveur, cherchez :

- `[core] Activation` — `context_chars`, `has_context` (contexte mémoire injecté
  à l'ouverture) ;
- `[core] Rappel` — `query`, `candidates`, `injected` (combien de souvenirs
  effectivement injectés) ;
- `[gemini] memory_injected` — `chars` du bloc `[MÉMOIRE]` réellement envoyé au
  modèle (`send_client_content`, `turn_complete=False`).

Sous `ATHENA_DEBUG_DECISIONS=1` s'ajoutent les décisions de consolidation
(`[consolidation] Consolidation` et `ConsolidationDetail`) et les décisions
`[core]` (`ToolCall`, `ToolResult`, `ClientAudio`, …) ; aucun événement n'est
retiré du flux.

## 6. Déclencher une Consolidation manuellement

La Consolidation tourne aux créneaux `ATHENA_CONSOLIDATION_HOURS` (défaut 00:00 et
12:00) avec rattrapage. Pour la forcer sans attendre un créneau, effacez le
marqueur de dernière consolidation puis redémarrez le serveur : le
planificateur la rattrape au démarrage.

```sh
/home/cooper/Documents/perso/athena/.venv/bin/python - <<'PY'
import sqlite3
db = sqlite3.connect("athena_memory.db")   # ou ATHENA_MEMORY_DB
db.execute("DELETE FROM consolidation_state")
db.commit()
PY
python -m server
```

La trace `[consolidation] Consolidation` doit afficher `facts_extracted` /
`facts_stored` non nuls, puis les faits extraits doivent être rappelables (étape
4). Il est aussi possible d'appeler directement la routine (voir « Résultat du
run », étape 6).

## 7. Inspecter la base et l'injection `[MÉMOIRE]`

```sh
/home/cooper/Documents/perso/athena/.venv/bin/python - <<'PY'
import sqlite3
db = sqlite3.connect("athena_memory.db")
print("faits:", db.execute(
    "SELECT content, pinned FROM facts WHERE user_id='owner' ORDER BY rowid"
).fetchall())
print("journal:", db.execute(
    "SELECT role, content FROM journal WHERE user_id='owner' ORDER BY rowid"
).fetchall())
print("dernière consolidation:", db.execute(
    "SELECT key, value FROM consolidation_state"
).fetchall())
PY
```

Le texte exact du bloc `[MÉMOIRE]` n'est pas journalisé par l'adaptateur (seul le
nombre de caractères l'est) ; pour le reproduire, cherchez les candidats dans le
Journal et la mémoire long terme :

```sh
/home/cooper/Documents/perso/athena/.venv/bin/python - <<'PY'
import asyncio
from server.adapters.memory import SentenceTransformerEmbedder, SqliteMemoryStore

async def main():
    emb = SentenceTransformerEmbedder("all-MiniLM-L6-v2")
    store = SqliteMemoryStore(db_path="athena_memory.db", embedder=emb)
    for hit in await store.recall("owner", "ma question"):
        print(f"{hit.distance:.3f} [{hit.source}] {hit.text}")

asyncio.run(main())
PY
```

## Vérification automatisée

```sh
python -m pytest protocol/tests server/tests client/tests -q
```

Les tests couvrent le port `MemoryStore`, l'embeddage hors boucle, la politique de
Rappel (seuil, dédup, troncature à `recall_k`), les Tools mémoire, la
Consolidation (fenêtre, dédup, rétention, marqueur) et le câblage de la racine.

## Résultat du run

**Date** : 2026-10-11 · **Machine** : poste de dev Linux · **Python** : 3.12.13
(venv partagée `/home/cooper/Documents/perso/athena/.venv`) ·
`sentence-transformers` 6.1.0, `torch` 2.14.1+cpu, `sqlite-vec`.

**Ce qui a été exécuté pour de vrai** (chemin non-voix, audio/VAD humain non
automatisable) :

1. **Boot du vrai serveur** — `PYTHONPATH=. GEMINI_API_KEY=smoke-test-dummy
   ATHENA_MEMORY_DB=… WS_PORT=8790 python -m server`. Démarrage propre,
   réchauffement du modèle local, `started`, Consolidation de rattrapage (base
   vide), arrêt `SIGINT` code 0 :

   ```
   INFO  [logging] sentence_transformers.base.model {"message": "Loading SentenceTransformer model from sentence-transformers/all-MiniLM-L6-v2."}
   INFO  [logging] websockets.server {"message": "server listening on 0.0.0.0:8790"}
   INFO  [app] started {"state": "Idle"}
   INFO  [consolidation] Consolidation {"duplicates": 0, "facts_extracted": 0, "facts_stored": 0}
   DEBUG [consolidation] ConsolidationDetail {"purged": 0, "since": null, "transcript_chars": 0}
   INFO  [app] stopped
   ```

2. **Activation sur Session réelle** — client WebSocket réel (`Register` +
   `SessionStart`) contre le vrai serveur, clé volontairement invalide : la trace
   `Activation` est émise immédiatement, la poignée de main Gemini échoue
   proprement (pas de blocage), la Session est fermée et le client reçoit
   `SessionEnded` :

   ```
   ERROR [gemini] connection_failed {"connection_id": "s1", "error": "1007 None. API key not valid. Please pass a valid API key."}
   INFO  [core] SessionRequested {"client_id": "f00e2614…", "connection_id": "s1", "from_state": "Idle", "to_state": "LiveConnecting", …}
   INFO  [core] Activation {"connection_id": "s1", "context_chars": 0, "has_context": false, "user_id": "owner"}
   INFO  [core] LlmFailed {"cause": "error", …, "reason": "ERROR", "to_state": "Closing"}
   INFO  [core] SessionClosed {"cause": "error", "reason": "ERROR", "to_state": "Idle"}
   ```

   (`context_chars: 0` attendu : base neuve. Le vrai appel Gemini Live avec clé
   valide + voix humaine est **différé à un humain**, cf. Limites.)

3. **Activation + Rappel de bout en bout, adaptateurs réels** — vrai
   `App` (transport WebSocket + Coordinator + `SqliteMemoryStore` réel +
   `SentenceTransformerEmbedder` réel + `ToolRegistry`), Gemini *stubbé* par un
   `live_connect` factice (pas de réseau). Fait épinglé
   « Camille habite à Lyon », fait durable « Les parents de Camille habitent à
   Nantes », tour « Je m'appelle Camille. » ; puis transcription utilisateur
   « Où habitent mes parents ? ». Traces observées :

   ```
   INFO [core] Activation {"connection_id": "s1", "context_chars": 287, "has_context": true, "user_id": "owner"}
   INFO [core] Rappel     {"candidates": 4, "connection_id": "s1", "injected": 2, "query": "Où habitent mes parents ?"}
   INFO [gemini] connected       {"connection_id": "s1", "model": "gemini-fake", "voice": "Zephyr"}
   INFO [gemini] memory_injected {"chars": 80, "connection_id": "s1"}
   ```

   Bloc réellement injecté via `send_memory` (intercepté sur la session factice) :

   ```
   [MÉMOIRE]
   - Où habitent mes parents ?
   - Les parents de Camille habitent à Nantes
   ```

   Le fait durable pré-existant est bien rappelé et injecté. *Observation* : la
   question courante de l'utilisateur apparaît aussi comme souvenir rappelé
   (distance ~0), car le noyau journalise l'énoncé **avant** le Rappel — comme
   dans le legacy.

4. **Mémorisation explicite + Activation + Rappel + Consolidation, store réel** —
   même vrai store/embedder. `save(pinned=True)` → profil épinglé présent dans
   `session_context` ; `save(pinned=False)` → fait rappelé :

   ```
   Profil de l'utilisateur (faits épinglés, toujours actifs):
   - L'utilisateur s'appelle Camille
   …
   [fact] dist=0.2416 'Camille habite à Lyon'   (recall « Où habite Camille ? »)
   ```

5. **Round-trip des Tools mémoire** — vrai `Coordinator` + vrai
   `ToolRegistry` (`register_memory_tools`) + vrai store/embedder. L'événement
   `LlmToolCall` est simulé (le modèle n'est pas joignable), le reste est réel :

   ```
   == save_memory kind=profile ==
     -> trace [core] ToolCall   {… "name": "save_memory"}
     -> SendToolResultToLlm name=save_memory result={'status': 'ok', 'kind': 'profile', 'pinned': True}
     -> trace [core] ToolResult {… "name": "save_memory", "status": "ok"}
     session_context contient bien le profil épinglé.
   == save_memory kind=fact ==  -> result={'status': 'ok', 'kind': 'fact', 'pinned': False}
     rappel fact -> ['Camille habite à Lyon', "L'utilisateur s'appelle Camille"]
   == forget_memory ==          -> result={'status': 'removed'}
     fait supprimé ; profil toujours présent: True
   ```

6. **Consolidation réelle (extracteur FAKE)** — vraie routine `Consolidation`
   (fenêtre Journal, dédup, écriture long terme, marqueur) avec un extracteur
   déterministe, faute de clé/réseau pour `GeminiFactExtractor` :

   ```
   report: {'ran': True, 'extracted': 2, 'stored': 2, 'duplicates': 0, 'purged': 0, 'transcript_chars': 31}
   [fact] dist=0.4874 'Camille est développeuse Python'   (rappelable après consolidation)
   DB: journal=1, facts=4, consolidation_state=1
   ```

   L'extracteur Gemini **réel** sans clé renvoie `[]` (échec réseau attendu) :
   `GeminiFactExtractor(api_key="", …).extract(...) -> []`.

7. **Déclenchement manuel de la Consolidation** — non rejoué séparément dans ce
   run : le rattrapage au démarrage a été observé (étape 1, base vide → 0 fait)
   et la routine a été exercée directement (étape 6 ci-dessus). La procédure
   manuelle de la section « 6. Déclencher une Consolidation manuellement »
   (effacer `consolidation_state` + redémarrer) reste à valider sur une base
   peuplée par un humain parlant.

**Différé à un humain (non automatisable ici) et pourquoi** :

- **Session vocale complète** (micro + enceinte + parole, mémorisation prononcée,
  redémarrage, rappel entendu à l'oreille) : `/dev/snd` existe mais il n'y a pas
  de voix automatisable ; non reproduit.
- **Gemini Live réel et `GeminiFactExtractor` réel** : aucune `GEMINI_API_KEY`
  valide disponible dans l'environnement ; les appels réseau sont donc stubbés
  (Live) ou remplacés par un extracteur déterministe (Consolidation).
- **Entrelacement `send_client_content` / `send_realtime_input`** en Session
  vocale réelle (caveat du spike #25) : à confirmer avec une clé valide.

**Vérification automatisée** :

```
$ PYTHONPATH=. .venv/bin/python -m pytest protocol/tests server/tests client/tests -q
373 passed
```

Aucun bug bloquant rencontré ; la seule particularité observée (auto-rappel de
l'énoncé courant, cf. étape 3) reproduit le comportement legacy et n'a pas été
modifiée.
