# La mémoire est du noyau, son stockage est un adaptateur

La **politique** mémoire (Journal, mémoire long terme, Consolidation, Activation, Rappel,
Faits épinglés) appartient au noyau ; le **stockage** (SQLite + vecteurs) est un adaptateur
derrière un port `MemoryStore`. La mémoire n'est jamais un plugin et ne peut pas être
désactivée. Réaffirme les ADR-0007 et 0009 de legacy.

## Considérations

- Raison : la mémoire est trop couplée au system prompt et au cycle de vie pour être
  optionnelle ; mais le noyau reste sans I/O, donc la persistance est branchée.
