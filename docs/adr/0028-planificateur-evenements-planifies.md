# Planificateur : Événements planifiés et annonce sans jugement

## Status

accepted

Amende l'ADR-0007 : la fenêtre de jugement ne s'ouvre plus sans aucun Client connecté, et
une **Occurrence** du Planificateur est annoncée sans passer par le juge. Amende l'ADR-0013 :
l'instruction d'Annonce peut recevoir le contenu planifié comme **fait**, sans phrase fournie
par un juge. Précise la fenêtre d'une Occurrence seule (0 s), la section `[CONTEXTE TEMPOREL]`
de l'Annonce et le sens d'`AnnouncementSilent`.

## Contexte

Athena ne peut aujourd'hui être interpellée que par une Source externe (Webhook, plugin, Console)
ou par l'utilisateur. Rien ne lui permet de se réveiller elle-même à une heure choisie. Le
besoin est double : des rappels ponctuels (« rappelle-moi ça dans 10 min ») et des rendez-vous
récurrents (« chaque lundi à 9 h, mon point hebdo »). L'utilisateur veut aussi qu'Athena puisse
créer, modifier et supprimer ces entrées depuis une conversation.

Le noyau possède déjà ce qu'il faut pour transporter et annoncer un fait : le **Canal
d'événements** unique (ADR-0010), la **File d'attente d'Événements** bornée (ADR-0007) et la
**Session automatique** d'Annonce (ADR-0013). Il manque un émetteur interne et un stockage
durable. La Consolidation planifie bien un traitement nocturne, mais elle n'émet aucun
Événement : c'est un processus interne, pas une Source.

## Décision

### Un composant du noyau

- Le **Planificateur** est un composant du noyau, comme la mémoire : toujours construit, actif
  dès que le runtime Gemini l'est, indépendant de tout plugin.
- Il possède les **Planifications**, les persiste, calcule leur prochaine occurrence et la fait
  entrer dans le **Canal d'événements**.
- Une Planification est **ponctuelle** (un instant unique) ou **récurrente** (intervalle,
  quotidienne ou hebdomadaire). Elle est qualifiée par une règle structurée, jamais par une
  expression cron : le modèle remplit un schéma, pas une syntaxe à cinq champs.

### Persistance

- Les définitions vivent dans une base SQLite dédiée, `ATHENA_SCHEDULE_DB` (`schedule.db` par
  défaut), derrière un port `ScheduleStore` (`server/core/ports.py`), sur le modèle de
  `auth.db` et `settings.db` : `CREATE TABLE IF NOT EXISTS`, sans migration.
- Les instants sont stockés en **UTC** ; les règles quotidiennes et hebdomadaires sont
  interprétées dans le **fuseau local de l'instance**, exposé comme Réglage (défaut : fuseau
  système). Les heures absolues saisies sont donc locales.
- Une Planification **ponctuelle est effacée** dès que son Occurrence est émise ; l'historique
  est le Journal, pas le magasin. L'effacement suit l'émission : un arrêt entre les deux
  réémet la ponctuelle (au pire un doublon, jamais une perte silencieuse) et une Annonce
  restée muette ne la relance pas.

### L'Occurrence

- L'émission d'une Planification s'appelle une **Occurrence**. Elle entre dans le Canal avec la
  Source unique `planificateur` et le contenu figé de la Planification.
- Une Occurrence rejoint la **File d'attente d'Événements** comme tout fait, et attend le Repos.
  Elle n'est jamais injectée dans une Session active (ADR-0007).
- Une Occurrence **saute le jugement** : au Repos, elle ouvre directement l'Annonce. Dans un lot
  mixte, le juge ne voit que les Événements ordinaires ; les Occurrences sont annoncées quoi
  qu'il décide, et un seul tour d'Annonce porte l'ensemble.
- Le contenu de la Planification est transmis comme **fait** ; la phrase à prononcer n'est pas
  lue mot pour mot. L'instruction d'Annonce fait formuler le rappel par le modèle, comme pour
  un Événement ordinaire — un contenu piégé reste une donnée, jamais une instruction.

### Attendre un Client

- La fenêtre de jugement **ne s'ouvre plus** si aucun Client n'est connecté : les Événements et
  les Occurrences patientent dans la File d'attente, dans l'ordre d'arrivée. Cette règle vaut
  pour **toutes** les Annonces, pas seulement pour le Planificateur : il n'existe qu'un seul
  comportement.
- La présence se lit sur les Clients réellement connectés au transport, jamais sur le dernier
  `client_id` enregistré — qui peut être périmé.
- Un rappel sonné pendant une Session attend le Repos, comme tout Événement.

### Occurrences manquées

- Une Planification **ponctuelle** dont l'heure tombe pendant un arrêt (ou pendant
  l'Installation, sans runtime) est rattrapée **une fois**, en retard, au démarrage ou à
  l'activation.
- Une Planification **récurrente** ne rattrape pas : elle reprend au prochain créneau valide,
  sans rafale d'Annonces.
- Chaque occurrence abandonnée est tracée.

### Temps

- Précision à la **seconde** : « dans 10 min » doit être juste.
- Changement d'heure : une heure locale inexistante est **sautée** au prochain créneau valide ;
  une heure locale doublée n'est émise **qu'une fois**.

### Tools du noyau

- `create_schedule(content, when)`, `list_schedules()`, `update_schedule(id, content?, when?)`,
  `delete_schedule(id)`, noms anglais comme les Tools du noyau existants.
- `when` est exactement un des cas : `{kind:"once", delay_seconds}` ou `{kind:"once", at}`
  (heure locale `YYYY-MM-DDTHH:MM:SS`) ; `{kind:"interval", every_seconds}` ;
  `{kind:"daily", time:"HH:MM"}` ; `{kind:"weekly", days:[1..7], time:"HH:MM"}`.
- Le contenu est **figé à la création** : une Planification est une donnée, pas une requête
  rejouée à chaque occurrence. Seul `update_schedule` le change.
- L'heure locale courante est injectée dans le prompt système de chaque Tour, pour que le modèle
  convertisse « dans 10 min » et « lundi prochain » sans deviner.
- Bornes : au plus **200** Planifications, intervalle minimal **60 s**, contenu ≤ **4096**
  caractères, ponctuelle strictement future, récurrence sans fin.

### Surfaces

- En v1, seuls les Tools du noyau écrivent : aucune zone Console, aucune route d'API dédiée.
  Le contrat de données doit cependant rester lisible par une surface future.
- Sans runtime Gemini (Installation), rien ne s'annonce ; les Planifications restent persistées
  et suivent la règle des occurrences manquées.

### Fenêtre d'Annonce

- Une File qui ne porte **que** des Occurrences arme la fenêtre unique avec une échéance
  immédiate (0 s) : rien n'est à regrouper avec un fait externe, et l'Annonce part à la
  seconde prévue. Un lot **mixte** garde la fenêtre de regroupement de 5 s, pour que
  l'Événement ordinaire et l'Occurrence ne produisent qu'un seul tour.
- L'instruction d'une Annonce porte la section `[CONTEXTE TEMPOREL]` comme toute Session
  automatique : l'heure locale est celle de l'instance au moment de l'ouverture.
- `AnnouncementSilent` s'applique à **toute** Annonce qui se termine avant d'être devenue
  visible (modèle muet, échec, arrêt), une seule fois, sans relance. Une Occurrence d'un
  lot interrompu par une préemption, elle, est reportée (`OccurrenceDeferred`,
  `announcement_aborted`, cause `preempted`) et reste en tête de File.

## Pourquoi

- Le Canal unique et la File d'attente existent déjà : brancher le Planificateur dessus évite un
  second chemin d'Annonce et un second jeu de gardes (taille, dédup, plafond, Repos).
- Une règle structurée est plus fiable qu'une expression cron pour un modèle, et évite une
  dépendance de parsing, la gestion des cinq champs et les pièges de fuseau.
- Une Occurrence n'a pas à être jugée : l'utilisateur a demandé explicitement qu'on le lui
  rappelle. Le jugement reste le filtre des faits externes, pas des engagements pris.
- Attendre un Client supprime une perte silencieuse : aujourd'hui une Annonce sans auditeur est
  prononcée dans le vide et consommée.

## Alternatives écartées

- **Un plugin `agenda`** : la Planification doit fonctionner sans plugin chargé, comme la
  mémoire ; un plugin rendrait le rappel dépendant de son activation.
- **Des expressions cron** : dépendance, erreurs de syntaxe fréquentes côté modèle, fuseau et
  DST plus difficiles à tester ; la règle structurée couvre les cas demandés.
- **Juger les Occurrences comme les autres Événements** : un rappel demandé pourrait être tu
  silencieusement, ce qui contredit l'intention de l'utilisateur.
- **Annoncer sans Client connecté** (comportement actuel) : perte silencieuse, déjà constatée.
- **Rejouer toutes les occurrences manquées** : rafale d'Annonces au redémarrage, sans valeur.
- **Lire le contenu comme phrase d'Annonce** : un contenu piégé deviendrait une instruction
  prononcée (#81).

## Conséquences

- `server/core/events.py` accueille un type interne d'Occurrence ; `Coordinator` enfile les
  Occurrences dans la même File d'attente avec leur drapeau « toujours annoncer ».
- Le `Coordinator` reçoit une lecture de présence des Clients (seam injecté, sans import
  d'adaptateur) et n'ouvre plus la fenêtre sans auditeur.
- Un port `ScheduleStore` et son adaptateur SQLite sont ajoutés ; le Planificateur est construit
  dans `build_app` et son pump démarre avec le runtime, comme la Consolidation.
- Le glossaire gagne **Planificateur**, **Planification** et **Occurrence** ; la définition
  d'**Événement** porte l'exception des Occurrences.
- Les bornes sont des constantes du noyau (comme la File d'attente), pas des Réglages en v1.
