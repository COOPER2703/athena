# Annonce : une Session ordinaire qui connaît son Événement

## Status

accepted

Amende l'ADR-0012 : la Connexion d'Annonce n'est plus une Connexion minimale (instruction
dédiée, sans mémoire ni Tools) qui ne reçoit que la phrase du juge ; c'est une **Session
ordinaire** qui reçoit aussi les faits bruts de l'Événement.

## Contexte

L'ADR-0012 réduisait la Connexion d'Annonce au strict nécessaire : une instruction dédiée
(`announce-prompt.txt`), **sans Tools**, et un seul tour — la phrase produite par le juge
texte. Les faits bruts du lot n'atteignaient jamais le Live : ils n'étaient écrits qu'au
Journal, en rôle `event`, au moment de l'Annonce.

Conséquence à l'usage : après une Annonce, l'utilisateur qui répond « donne-moi plus de
détails » n'obtient rien. Dans son contexte, Athena n'a ni l'Événement lui-même (seulement sa
reformulation courte), ni le prompt système d'Athena, ni Tools, ni le contexte mémoire de
Session. Au moment où l'utilisateur lui parle, ce n'est plus tout à fait Athena.

## Décision

- La Connexion d'Annonce est une **Session ordinaire** : ouverte avec le prompt système
  (`system-prompt.txt`) augmenté de l'instruction d'Annonce (`announce-prompt.txt`) et du
  contexte mémoire de Session (Activation), avec les Tools du noyau. Le verdict reste celui du
  juge **texte** : la Connexion n'est toujours ouverte que si `announce=true`.
- L'instruction d'Annonce dit à Athena que la Session a été ouverte par un **Événement**, lui
  indique de prononcer la phrase fournie, puis de répondre normalement si l'utilisateur demande
  des détails.
- `SendAnnouncementToLlm` porte désormais **deux** contenus : les faits bruts du lot (`facts`),
  injectés en **contexte** (`turn_complete=false`) — comme le rappel mémoire, jamais prononcés —
  puis la phrase du juge (`text`), qui **complète le tour** (`turn_complete=true`) et déclenche
  la parole.
- Les faits du lot sont semés dans le dédoublonnage du Rappel : le premier tour utilisateur ne
  les réinjecte pas.
- Le timeout d'Annonce (`ANNOUNCE_TIMEOUT`) reste un **filet de sécurité** : une Connexion qui
  ne produit aucun son est refermée sans qu'aucune Session n'apparaisse.

## Pourquoi

- Le juge texte reste **séparable** du support audio : il décide de parler, le Live parle puis
  poursuit la conversation comme n'importe quelle Session.
- Fournir les faits bruts, plutôt que la seule reformulation du juge, rend le suivi possible
  sans second appel de jugement ni dépendance à la recherche sémantique.

## Alternatives écartées

- **Ne rien changer au Live et compter sur le Rappel** : les faits annoncés entrent bien au
  Journal en rôle `event` et sont recherchables, mais rien ne garantit leur récupération (seuil
  de distance, formulation de la question) et l'instruction d'Annonce ne dit pas à Athena d'en
  faire usage.
- **Envoyer les faits et la phrase dans un seul tour** : le modèle risque de réciter le bloc
  brut ; deux contenus séparent proprement « contexte » et « ce qu'il faut dire ».

## Conséquences

- Le port `LlmSocket.send_announcement` gagne un argument `facts` ; l'adaptateur émet une trace
  `announcement_facts_sent` puis `announcement_sent`.
- Le noyau (`_start_announcement`) devient **async** : il interroge le port mémoire pour
  l'Activation, comme `_on_session_requested`.
- `announce-prompt.txt` est réécrit : il décrit une Session ouverte par un Événement, pas un
  simple ordre d'annonce.
