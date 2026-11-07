# Fin de Session décidée par le modèle

## Status

accepted

Amende l'ADR-0008 : le Tool du noyau `end_session` existe désormais ; il ne reste plus
« plus tard la fin de Session ». Ajoute une note de suivi à l'ADR-0017 : les Sessions
d'Annonce gardent `tools=()` dans cette tranche, mais cette garde structurelle est
**provisoire** — voir « Décision à réviser ». Ne modifie aucun autre ADR.

## Contexte

Une Session ne se terminait que par un signal de fin de Gemini, une défaillance, un
remplacement, ou le timeout d'inactivité de 30 s. Quand l'utilisateur dit clairement « au
revoir », Athena répond, puis la Session reste ouverte 30 s : le Client reste `ACTIVE`,
continue d'envoyer l'audio du micro, et la Session suivante est retardée. L'utilisateur
doit attendre ou interrompre à la main.

Le motif de fin ne permettait pas non plus de distinguer une fin **décidée par le modèle**
d'une fermeture Gemini (`NORMAL`) ou d'un timeout (`TIMEOUT`). L'exploitant ne pouvait donc
pas lire dans les Traces pourquoi Athena avait arrêté d'écouter.

## Décision

### Un Tool du noyau, la politique dans le Coordinator

- Le Tool `end_session` appartient au noyau, propriétaire `core` : un plugin ne peut pas
  prendre ce nom, et une collision reste fatale au démarrage (ADR-0014).
- Il n'a **aucun paramètre** (`{"type": "object", "properties": {}}`). Son handler rend
  `{"status": "ok"}` et ne fait rien d'autre : la politique de fermeture reste dans le
  Coordinator (ADR-0008). La description du Tool et le prompt système lui donnent la même
  consigne — dire d'abord au revoir, ne pas clore pendant une demande en cours.
- Il est déclaré dans `OpenLiveConnection.tools` de toute Session ordinaire, y compris
  avant que la Session ne devienne visible.
- Il est **absent** des Sessions d'Annonce, qui conservent `tools=()` (ADR-0017).

### Clôture en attente

Le noyau ne ferme pas brutalement à l'appel du Tool. Il ouvre une **Clôture en attente** :

1. il exécute le handler et rend le résultat au modèle (`SendToolResultToLlm`) ;
2. il arme une **grâce de 3 s** sur la clé de timer de la Connexion, qui remplace le timeout
   d'inactivité ;
3. il arme un **plafond de 10 s** depuis l'appel du Tool, sur une clé de timer réservée
   dérivée de la Connexion.

Chaque `LlmAudio` pendant la clôture relance la grâce de 3 s, jamais le timeout de 30 s :
Athena finit sa phrase d'adieu sans être coupée. À l'échéance de la grâce, la Session se
termine par le chemin de fin unique (`_teardown`) avec le motif `COMPLETED`.

Le plafond protège l'inverse : si le modèle parle encore à T+10 s, le noyau renonce à
fermer. La clôture est annulée, une Trace `SessionEndCanceled` porte
`cause=audio_overrun`, et le timeout d'inactivité de 30 s est réarmé. Le plafond est mesuré
depuis l'appel du Tool ; « encore en attente à T+10 s » signifie que le modèle a parlé
jusqu'à environ 10 s.

Un `ClientInterrupt` pendant la clôture l'annule aussi : Trace `SessionEndCanceled` avec
`cause=barge_in`, timeout de 30 s réarmé, puis barge-in traité normalement. Un second appel
d'`end_session` pendant la clôture rend un succès sans prolonger la grâce ni le plafond : on
ne maintient pas la Session ouverte en rappelant le Tool. Un appel hors Session active
(`IDLE`, `LIVE_CONNECTING`, `CLOSING`) rend une erreur au modèle et n'arme aucun timer.

Une `LlmTranscription` **utilisateur** pendant la clôture ne relance plus le timeout de
30 s et n'annule pas la clôture : une interjection purement orale ne sauve pas la Session,
et une conversation de fond ne prolonge pas la fermeture. Le tour est tout de même
journalisé et le Rappel fonctionne comme avant. C'est un changement assumé du comportement
antérieur, où toute parole utilisateur réarmait l'inactivité.

Les chemins de fin existants restent inchangés : timeout (`TIMEOUT`), Client parti
(`CLIENT_GONE`), arrêt (`SHUTDOWN`), erreur (`ERROR`), fermeture Gemini (`NORMAL`),
remplacement (`REPLACED`). Tout autre teardown annule le plafond et efface l'état de
clôture ; une nouvelle Session repart d'un état vierge, sans timer périmé capable de la
fermer.

### Protocole et observabilité

- `SessionEndReason.COMPLETED = 7` est **additif** et **stable** : aucune valeur existante
  n'est déplacée ni réutilisée. Le Client desktop continue d'ignorer le motif et revient à
  `LISTENING` sur toute fin de Session.
- Les Traces `ToolCall`, `ToolResult` et `SessionClosed` suffisent pour l'appel, le résultat
  et la fin (`SessionClosed` porte déjà `reason=COMPLETED`). `SessionEndCanceled` ajoute la
  Connexion, la Session, le motif et la cause (`audio_overrun` ou `barge_in`).
- Aucune Trace ne contient l'audio ni le contenu de la conversation.

## Décision à réviser

Les Sessions d'Annonce restent **sans Tools** dans cette tranche, y compris sans
`end_session`. C'est une garde **structurelle** voulue par l'ADR-0017, pas un oubli : le
contenu externe qui déclenche une Annonce est une donnée non fiable, et aucun Tool ne doit
pouvoir transformer une injection en action.

Cette décision est **provisoire**. Dès qu'un **système de protection contre les injections**
existera, les Sessions d'Annonce devront exposer **les mêmes Tools qu'une Session
ordinaire** — `end_session` compris. La révision devra d'abord livrer ce système, puis
retirer la liste vide ; elle ne doit pas être faite par simple symétrie.

## Pourquoi

- **La latence perçue.** « Au revoir » ferme la Session en quelques secondes au lieu de
  laisser le micro actif 30 s.
- **L'adieu n'est pas coupé.** La grâce est relancée par chaque audio du modèle, et le
  plafond empêche qu'une longue réponse soit interrompue trois secondes après sa dernière
  phrase.
- **Le comportement reste déterministe.** Grâce fixe, plafond fixe, aucun retry caché ;
  l'échec de la clôture anticipée est toujours sûr : au pire, on retombe sur le timeout de
  30 s, jamais sur une fermeture abusive.
- **La politique reste dans le noyau.** Un Tool propriétaire `core` ne peut pas être écrasé
  par un plugin, et le handler ne connaît aucune règle de fermeture.
- **La fin est lisible.** `COMPLETED` distingue dans les Traces une fin décidée par le
  modèle de `NORMAL` et de `TIMEOUT` ; `SessionEndCanceled` explique pourquoi une Session
  est restée ouverte.

## Alternatives écartées

- **Fermer à l'appel du Tool.** Coupe la phrase d'adieu et le playback du Client.
- **Un simple timer fixe, sans réarmement.** Coupe une réponse longue ; ou, s'il est trop
  long, ne rend pas la main assez vite.
- **Laisser un plugin posséder `end_session`.** La politique de fermeture sortirait du
  noyau et un plugin pourrait détourner le nom.
- **Annuler la clôture sur une interjection purement orale.** Un bruit ou une conversation
  de fond garderait la Session ouverte ; seul un `ClientInterrupt` explicite annule.
- **Un `SessionEnd` initié par le Client.** Le message existe dans le Protocole mais reste
  hors périmètre de cette tranche.

## Conséquences

- Un nouveau Tool du noyau, un nouveau motif de fin et une nouvelle Trace : le registre
  gagne `end_session` et `Registered.server_tools` grandit d'un nom.
- Deux timers supplémentaires réutilisent `StartIdleTimer` / `CancelIdleTimer` : aucune
  commande ni aucun événement nouveau dans le noyau.
- La Session d'Annonce conserve sa dette documentée : aucun Tool avant un système
  anti-injection, puis les Tools ordinaires, `end_session` compris.
- Le prompt système gagne une section « Fin de Session », partagée avec les Annonces : elle
  précise que le Tool peut être indisponible et qu'il ne faut alors pas le mentionner.
- Le Client desktop ne change pas : `COMPLETED` est d'abord une information de protocole et
  de Trace.
