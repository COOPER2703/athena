# Fin de Session immédiate sur `end_session`

## Status

accepted

Supersède le mécanisme de **Clôture en attente** de l'ADR-0020 : la grâce de 3 s
et le plafond de 10 s disparaissent. Le reste de l'ADR-0020 reste en vigueur —
`end_session` appartient au noyau (`core`), il est refusé hors Session ordinaire,
il est absent des Sessions d'Annonce, et le motif de fin `COMPLETED` est stable.

## Contexte

L'ADR-0020 fermait une Session en deux temps : `end_session` armait une grâce de
3 s après le dernier audio du modèle, et un plafond de 10 s **annulait** la
clôture si le modèle parlait encore. Le plafond n'était donc pas un délai, mais
une renonciation : une longue réponse laissait la Session ouverte et le timeout
d'inactivité de 30 s reprenait la main.

L'utilisateur veut l'inverse : dès qu'il demande la fin, ou dès qu'Athena la
décide, la Session doit se terminer. La grâce de 3 s reste un délai perçu, et le
plafond peut annuler une fermeture légitime.

Fermer à l'appel du Tool avait été écarté par l'ADR-0020 parce que cela coupe la
phrase d'adieu et le playback du Client. Le vrai conflit n'est pas « attendre ou
fermer » : c'est « fermer côté serveur » contre « finir de jouer côté Client ».

## Décision

- Dans une Session **ordinaire active**, un appel réussi d'`end_session` ferme la
  Session **immédiatement**, par le chemin de fin unique du Coordinator, avec le
  motif `COMPLETED`. Aucune grâce, aucun plafond, aucun état de clôture en
  attente, aucune trace `SessionEndCanceled`.
- Le handler du Tool reste un simple accusé de réception. Le résultat est émis
  avant la fermeture, et les Traces `ToolCall` / `ToolResult` restent inchangées.
- `SessionEnded` est envoyé **même si la Session n'était jamais devenue visible** :
  un Client qui a démarré une Session doit toujours revenir à `LISTENING`.
- Côté Client, `SessionEnded` ramène immédiatement l'état à `LISTENING` mais **ne
  jette plus la file de lecture** : l'audio déjà reçu s'achève. Un nouveau tap,
  ou un `SessionStarted` reçu pendant ce drain, vide le reliquat avant de
  commencer. Une connexion perdue, elle, jette la file devenue périmée.
- Le timeout d'inactivité de 30 s reste inchangé. Le barge-in `ClientInterrupt`
  reste inchangé lui aussi : il n'annule simplement plus de clôture, puisqu'il
  n'y en a plus.
- Le modèle juge la **clôture sémantique**, pas une formule exacte : « au revoir »,
  « non c'est bon en fait », « nickel, merci » ou un acquiescement final
  suffisent. La garde du doute reste : pas de clôture pendant une demande en
  cours, et l'adieu est prononcé **avant** l'appel puisque la fermeture est
  immédiate.
- Le Protocole partagé ne change pas : `COMPLETED` existe déjà et aucune trame
  n'est ajoutée.

## Pourquoi

- **La latence perçue.** La Session se ferme au moment de la décision, sans
  attendre 3 s ni risquer une annulation à 10 s.
- **L'adieu n'est pas coupé.** Il n'est plus protégé par un timer serveur, mais
  par le drain côté Client : ce qui a déjà été reçu est joué.
- **Le comportement est plus simple et déterministe.** Aucun timer, aucune
  annulation, aucun état intermédiaire : un appel, une fermeture.
- **Le coût d'un faux positif est assumé.** Comme la fermeture est immédiate, la
  description du Tool et le prompt système conservent la consigne « en cas de
  doute, laisse la Session ouverte ».

## Alternatives écartées

- **Retirer seulement le plafond de 10 s en gardant la grâce de 3 s.** Ce n'est
  pas instantané : la Session reste ouverte 3 s après le dernier mot.
- **Fermer à l'appel et jeter la file de lecture du Client.** Coupe l'adieu déjà
  prononcé ; c'est l'alternative que l'ADR-0020 avait écartée, et elle reste
  mauvaise.
- **Garder une fenêtre annulable après l'appel.** Contredit la fermeture
  immédiate et réintroduit l'état de clôture en attente.
- **Un `SessionEnd` initié par le Client.** Le message existe dans le Protocole,
  mais un contrôle Client direct reste une fonctionnalité distincte, hors de
  cette tranche.
- **Couper net sur une demande vocale explicite d'arrêt.** Cela demanderait un
  signal distinguant « arrête » d'« au revoir » ; le tap barge-in couvre déjà le
  vrai stop immédiat.

## Conséquences

- Le Coordinator perd la grâce, le plafond, la clé de timer réservée, l'état
  `_end_session_pending` et le chemin d'annulation.
- Le Client desktop gagne le drain de l'adieu et le flush du reliquat au départ
  suivant.
- Les tests du `Coordinator` et du Client décrivent la fermeture immédiate et le
  drain, plus aucun timer de clôture.
- La **Clôture en attente** disparaît du glossaire ; `COMPLETED` reste le motif
  d'une fin décidée par le modèle.
