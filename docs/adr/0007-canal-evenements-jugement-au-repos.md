# Canal d'événements : juger au Repos, regrouper les bursts

## Status

accepted

Amendé par l'ADR-0012 : le verdict de jugement vient d'un modèle texte, plus du silence du
modèle Live.

Amende la pratique décrite par le glossaire (une Notification injectée dans la Session
active) et précise l'ADR-0019 de `legacy/`. La forme du transport (une seule file) est
actée par l'ADR-0010 ; la fin de Session déclenchée par un Tool du noyau est hors périmètre.

## Contexte

Les Sources (plugins, applications externes) poussent des Événements dans le Canal
d'événements. On imaginait jusqu'ici qu'un Événement reçu pendant une Session active serait
injecté comme message système dans cette Session (la « Notification »). À l'usage, cette
injection coupe la parole en cours (barge-in involontaire) et mélange deux contextes dans la
même Connexion Live : Gemini répond déjà, et le fait réinjecté en plein vol produit un
comportement imprévisible.

## Décision

- Le noyau ne juge un Événement qu'au **Repos** : aucune Connexion Live ouverte, aucune
  Session visible. Un Événement n'est **jamais** injecté dans une Session active, ni dans une
  Connexion Live en vol. Les entrées des adaptateurs (audio, cycle de vie, horloge), elles,
  sont traitées immédiatement quel que soit l'état.
- Hors Repos, les Événements sont conservés dans la **File d'attente d'Événements** — un état
  du noyau, pas une seconde file — et jugés au retour au Repos, dans l'ordre d'arrivée.
- Au Repos, la **fenêtre de regroupement** est **paresseuse et fixe** : elle s'ouvre au
  premier Événement reçu alors que le noyau est au Repos, dure **5 s**, et tous les Événements
  reçus avant l'échéance sont jugés ensemble, en un seul tour, comme des faits distincts
  étiquetés par Source — pour ne pas ouvrir une Connexion Live par Événement.
- La File d'attente est **plafonnée à 120 Événements** ; au-delà, les plus anciens sont jetés
  à l'arrivée et le drop est tracé. Elle est **éphémère** : les Événements non jugés sont
  perdus au redémarrage, et le drop est tracé.
- À l'entrée dans la fenêtre, le noyau **écarte les doublons** `(source, contenu)` déjà
  présents dans cette fenêtre, et **rejette** tout Événement dont le contenu dépasse une
  taille maximale (rejet tracé). Le plafond global suffit : pas de quota par Source.
- Un jugement ouvre **une seule Connexion Live**, avec une **instruction de jugement dédiée**
  (versionnée, sans Rappel mémoire ni Tools) ; le lot est transmis comme contenu de tour.
- Le verdict est reconnu **par timeout seul**. Si le LLM parle, la Connexion Live devient une
  **Session automatique** (Annonce proactive), que le Client reconnaît. S'il se tait, elle est
  refermée au bout d'un **timeout de jugement dédié**, plus court que l'inactivité d'une
  Session, sans qu'aucune Session n'apparaisse.
- **L'utilisateur gagne toujours** : un Wake word pendant la fenêtre l'annule (les Événements
  restent en attente) ; un Wake word pendant un jugement le préempte, et les faits du lot
  retournent dans la File d'attente.
- Une seule Connexion Live à la fois.

## Pourquoi

- Pas de barge-in involontaire : un Événement ne peut pas couper une parole en cours.
- Pas de mutation d'une Connexion Live en vol : le LLM n'a pas commencé à répondre quand le
  fait suivant arrive.
- Coût borné : regrouper un burst réduit le nombre d'appels Gemini, et la fenêtre paresseuse
  garantit qu'un Événement isolé est tout de même jugé.

## Alternatives écartées

- Injecter l'Événement dans la Session active (Notification) : imprévisible, rejeté.
- Un jugement par Événement, sans fenêtre : N allers-retours Gemini pour un burst.
- Une fenêtre glissante (chaque arrivée relance 5 s) : peut affamer indéfiniment sous flux
  continu ; une fenêtre roulante juge des lots vides.
- Détecter le silence sur un signal de fin de tour Gemini (`turn_complete`) : le signal existe
  côté SDK mais l'ADR retient le timeout seul, avec un timeout de jugement dédié.
- Rendre la File d'attente durable : inutile pour un fait chaud, et complexe.

## Conséquences

- Le terme « Notification » du glossaire disparaît ; le message protocole `Notification` est
  supprimé, et `SessionEnded.Reason.SILENT_EVENT` reste **réservé, jamais produit**.
- La File d'attente et la fenêtre sont des responsabilités du **noyau** ; le Canal
  (transport) appartient à la composition root (ADR-0010).
- Un Événement annoncé est écrit au Journal en rôle `event` ; tout le tour de jugement (fait
  et parole d'annonce) est exclu de la Consolidation. Un Événement tu n'entre pas au Journal.
- Un Événement peut attendre la fin d'une Session avant d'être jugé.
