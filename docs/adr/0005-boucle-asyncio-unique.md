# Une seule boucle asyncio, aucun thread par plugin

Tout le serveur vit dans une **boucle asyncio unique** ; les intégrations (Matrix, Home
Assistant, …) sont des adaptateurs `async`, sans thread ni boucle dédiée. Les jobs longs
tournent en tâches d'arrière-plan et rendent leur résultat via le **Canal d'événements** :
le noyau ne bloque jamais, et un job terminé déclenche une Annonce proactive — sous réserve
du jugement hors Session de l'ADR-0007.

## Considérations

- Raison : supprimer les bugs de teardown et d'état global du modèle « un thread par
  plugin » de legacy, et garantir qu'un travail long (ex. développement assisté) ne bloque
  ni la boucle ni la parole.
- Options rejetées : statu quo legacy (thread + boucle par plugin externe).
