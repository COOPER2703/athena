# Canal d'événements unique

## Status

accepted

## Contexte

Le noyau reçoit deux familles de messages : les entrées des adaptateurs (audio du Client,
signaux de la Connexion Live, horloge d'inactivité) et les Événements poussés par les
Sources (plugins, applications externes). L'ADR-0007 exige que les Événements soient jugés
au Repos, donc conservés hors Repos — ce qui invite à les faire transiter par une file
distincte des signaux internes.

## Décision

- Le noyau n'a **qu'un seul Canal d'événements** : une file unique par laquelle entrent, dans
  l'ordre d'arrivée, les entrées des adaptateurs **et** les Événements des Sources. Un seul
  point de traitement, un élément à la fois.
- Les entrées des adaptateurs sont traitées **immédiatement**, quel que soit l'état du noyau.
  Le seul traitement différé est celui des Événements, réalisé par la File d'attente
  d'Événements (ADR-0007), qui est un **état** du noyau et non une seconde file.
- Le tampon de la File d'attente vit dans le `Coordinator` ; la composition root ne fait que
  transporter. Le `Coordinator` reste l'unique machine à états : il n'est jamais appelé en
  concurrence.

## Pourquoi

Le jugement dépend de l'état du noyau (le Repos), donc les deux familles de messages doivent
être sérialisées par la même machine. Deux files forceraient soit deux consommateurs
concurrents (courses sur l'état — le bug que l'ADR-0005 a supprimé), soit une tâche de fusion
avec une politique d'ordre entre files : la file unique, réintroduite en moins lisible.

## Alternatives écartées

- Deux files (adaptateurs / Sources) : ajoute une course ou une étape de fusion, sans gagner
  la séparation voulue — celle-ci est une **politique** (le gate du Repos), pas un transport.
- Un composant d'intake séparé émettant `EventsReady` : défendable, mais l'intake a besoin de
  l'état de Repos que le `Coordinator` détient déjà, et le découpage ne se rentabilise pas.
- Deux vocables pour les deux familles de charge utile : n'apporte rien, le glossaire
  distingue déjà les Événements des Sources des entrées des adaptateurs.

## Conséquences

- « Canal d'événements » désigne la file unique ; le mot « Événement » reste réservé au fait
  externe poussé par une Source.
- L'union interne `Event` (`server/core/events.py`) n'est pas renommée ; elle accueille un
  type de plus pour les Événements entrants.
