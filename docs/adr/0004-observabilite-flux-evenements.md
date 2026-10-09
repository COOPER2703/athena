# Observabilité : un flux d'événements structuré émis par le noyau

Le noyau et les adaptateurs émettent une **chronologie unique d'événements structurés** :
transitions de Session, décisions (jugement d'Événement, Activation, Rappel), appels de
Tools, et logs d'adaptateurs y compris des bibliothèques tierces. Le rendu texte et la
console web en sont deux consommateurs. Rien n'est loggé en dehors de ce flux.

## Considérations

- Raison : reconstituer une **causalité unique** (quoi s'est passé, quand, pourquoi) pour
  situer un bug, ce qui est un objectif de premier ordre de la réécriture.
- Options rejetées : deux canaux séparés (logs texte + flux d'événements), qui obligent à
  recoller mentalement les morceaux ; le débogage ad hoc de `ATHENA_DEBUG_DECISIONS`.
