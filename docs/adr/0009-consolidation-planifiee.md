# Consolidation planifiée à 00:00 et 12:00, avec rattrapage

La **Consolidation** de la mémoire long terme tourne deux fois par jour, à heures fixes
(00:00 et 12:00, heure locale du serveur). Si le serveur était éteint au créneau, elle
**rattrape au démarrage** suivant ; la fenêtre traitée reste « depuis la dernière
Consolidation ». Remplace le « premier démarrage après minuit » de legacy.

## Considérations

- Pourquoi deux fois : réduire la quantité de Journal non consolidé et offrir deux points de
  consolidation par jour.
- Options rejetées : « premier démarrage après minuit » (un seul point quotidien, dépend du
  moment de démarrage) ; intervalle glissant (heures de consolidation imprévisibles).
- Conséquence : la Consolidation est une tâche d'arrière-plan planifiée du noyau, indépendante
  de l'activité de Session et sans appel bloquant pour la parole.
