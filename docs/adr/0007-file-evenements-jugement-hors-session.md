# File d'Événements : juger hors Session, regrouper les bursts

## Status

accepted

Amende la pratique décrite par le glossaire (une Notification injectée dans la Session
active) et précise l'ADR-0019 de `legacy/`.

## Contexte

Les Sources (plugins, applications externes) poussent des Événements dans le Canal
d'événements. On imaginait jusqu'ici qu'un Événement reçu pendant une Session active serait
injecté comme message système dans cette Session (la « Notification »). À l'usage, cette
injection coupe la parole en cours (barge-in involontaire) et mélange deux contextes dans la
même Connexion Live : Gemini répond déjà, et le fait réinjecté en plein vol produit un
comportement imprévisible.

## Décision

- Le noyau ne traite un Événement que lorsqu'il est **Idle** : aucune Connexion Live de
  jugement ouverte, aucune Session visible. Un Événement n'est **jamais** injecté dans une
  Session active, ni dans une Connexion Live en vol.
- Pendant un jugement ou une Session, les Événements sont **mis en file d'attente** et jugés
  au retour à Idle, dans l'ordre d'arrivée.
- Au repos, le noyau ouvre une **fenêtre de regroupement de 5 s** : les Événements reçus
  pendant cette fenêtre sont jugés ensemble, en un seul tour, comme des faits distincts
  étiquetés par Source — pour ne pas ouvrir une Connexion Live par Événement.
- La file est **plafonnée à 120 Événements** ; au-delà, les plus anciens sont jetés et le
  drop est tracé dans le flux d'observabilité.
- Une seule Connexion Live à la fois.

## Pourquoi

- Pas de barge-in involontaire : un Événement ne peut pas couper une parole en cours.
- Pas de mutation d'une Connexion Live en vol : le LLM n'a pas commencé à répondre quand le
  fait suivant arrive.
- Coût borné : regrouper un burst réduit le nombre d'appels Gemini.

## Alternatives écartées

- Injecter l'Événement dans la Session active (Notification) : imprévisible, rejeté.
- Un jugement par Événement, sans fenêtre : N allers-retours Gemini pour un burst.
- Fusionner un Événement dans une Connexion Live déjà ouverte : course avec la génération en
  cours du modèle.

## Conséquences

- Le terme « Notification » du glossaire disparaît.
- La file d'attente et la fenêtre de regroupement sont des responsabilités du **noyau**, pas
  des adaptateurs.
- Un Événement peut attendre la fin d'une Session avant d'être jugé.
