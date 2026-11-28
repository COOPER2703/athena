# Smoke test — T6 Planificateur (`python -m server` + `python -m client`)

Vérification manuelle de bout en bout du **Planificateur** (spec #191, ticket #195,
ADR-0028) : un rappel ponctuel demandé en conversation est émis dans le Canal d'événements à
l'heure prévue, attend le Repos et un Client connecté, puis ouvre une **Annonce sans
jugement** que le modèle formule à partir du fait. Voir `docs/smoke-test-t3-evenements.md`
pour le Canal ordinaire et `docs/smoke-test-t2-memoire.md` pour le Journal.

## Prérequis

Toutes les commandes se lancent **depuis la racine du dépôt**.

- Un `GEMINI_API_KEY` valide (modèle Live, juge, Consolidation).
- Le protocole généré : `python protocol/proto/generate.py` (ou `make generate`).
- Un micro et un haut-parleur accessibles au client (`pyaudio`).
- Un **Compte** et un **Jeton d'accès** `client` (Console → Jetons, ou
  `ATHENA_ACCESS_TOKEN` pour le Client desktop).
- **Un Client connecté** au moment du rappel : sans auditeur, l'Occurrence patiente dans la
  File d'attente et le report est tracé (`OccurrenceDeferred`, raison `no_client`).
- Un `.env` à la racine avec la clé, et éventuellement le fuseau de l'instance :

```dotenv
GEMINI_API_KEY=votre-cle
ATHENA_USER_ID=owner
# IANA : interprète « 9 h » et rend l'heure locale du contexte temporel.
# ATHENA_TIMEZONE=Europe/Paris
```

## 1. Démarrer le serveur et le Client

```sh
python -m server          # terminal 1
python -m client          # terminal 2, avec le Jeton saisi sur la page locale
```

Le serveur trace `started`, le transport enregistre le Client (`client_registered`). Le pump
du Planificateur tourne : il a réclamé les échéances manquées au démarrage
(`OccurrenceSkipped` pour une récurrence en retard, rattrapage unique d'une ponctuelle) puis
attend la prochaine.

## 2. Demander le rappel en conversation

Dites au Client :

> « Rappelle-moi d'appeler le plombier dans une minute. »

Le modèle appelle `create_schedule` ; le Tool rend `status: "ok"` avec l'échéance locale, et
la trace de création apparaît :

```text
INFO [schedule] ScheduleCreated {"id": "sch_…", "kind": "once"}
```

La création réveille le pump (`on_change`) : il recalcule son échéance, sans attendre la
précédente. Vérifiez la Planification en attente :

```sh
sqlite3 schedule.db 'SELECT id, content, next_occurrence FROM schedules;'
```

## 3. Attendre l'heure, puis vérifier l'émission

À l'échéance, le pump réclame la ligne, émet l'Occurrence, puis efface la ponctuelle
(l'effacement suit l'émission : un arrêt entre les deux réémet au pire une fois) :

```text
INFO [planificateur] OccurrenceEmitted {"schedule_id": "sch_…", "kind": "once", "due": …}
INFO [core] OccurrenceQueued {"schedule_id": "sch_…", "queued": 1}
INFO [core] EventWindowOpened {"events": 1}
INFO [core] EventJudge {"announce": true, "events": 1, "occurrences": 1, …}
```

- Aucun appel au juge : seuls les Événements ordinaires d'un lot mixte le sont.
- La fenêtre s'arme à **zéro seconde** pour une File qui ne porte que des Occurrences :
  l'Annonce part à la seconde prévue, pas cinq secondes plus tard.
- Le Client entend le rappel formulé par le modèle : le contenu de la Planification est un
  **fait** sous `[ÉVÉNEMENTS]`, jamais la phrase lue.

La ponctuelle a disparu du magasin :

```sh
sqlite3 schedule.db 'SELECT COUNT(*) FROM schedules;'   # 0
```

Le fait annoncé est au Journal en rôle `event` (donc exclu de la Consolidation) :

```sh
sqlite3 memory.db "SELECT role, content FROM journal ORDER BY rowid DESC LIMIT 4;"
# event | [planificateur] appeler le plombier
```

## 4. Rappel sans Client : report, puis rattrapage

Fermez le Client, demandez un rappel court (« dans trente secondes »), puis rouvrez le Client
avant l'échéance ou laissez-le fermé :

```text
INFO [core] OccurrenceDeferred {"occurrences": 1, "reason": "no_client"}
```

La File reste intacte ; au premier `ClientRegistered`, la fenêtre s'ouvre et le rappel part
avec le premier Client connecté. Si le noyau est occupé par une Session, l'Occurrence attend
le Repos sans l'interrompre.

## 5. Cas d'échec observables

- **Contenu trop long** (> 4096 caractères) : `OccurrenceDropped {reason: "too_large"}`,
  jamais mise en File.
- **File pleine** (120 faits en attente) : la plus ancienne est jetée à l'arrivée —
  `OccurrenceDropped {reason: "capacity"}` pour une Occurrence, `EventQueueDropped` pour un
  Événement.
- **Arrêt** : les faits en attente sont jetés et tracés (`reason: "shutdown"`), sans Annonce.
- **Modèle muet** : la Connexion d'Annonce se referme sans devenir visible →
  `AnnouncementSilent` une seule fois, sans relance, et rien n'entre au Journal.

## 6. Installation : aucun pump

Sans clé Gemini (instance en Installation), le serveur, la Console et les Réglages
répondent, les Tools de Planification persistent leurs lignes, mais **aucune Occurrence
n'est émise** : le Planificateur n'a pas de pump et aucune Annonce n'est tentée. La première
clé Gemini active le runtime et le pump à chaud ; la ponctuelle en retard est alors
rattrapée une fois.
