# Réécriture greenfield : noyau pur + adaptateurs

Le POC `legacy/` a accumulé une dette structurelle — `SessionManager` god object, état
global mutable, deux implémentations de dispatch — qu'un refactor en place ne corrigerait
qu'au prix d'un big bang. On réécrit à neuf un **noyau pur** (cycle de vie des Sessions,
mémoire, jugement des Événements), sans I/O, entouré d'**adaptateurs** injectés (transport,
Gemini, persistance, plugins, console). `legacy/` reste une référence vivante.

## Considérations

- Options rejetées : refactorer `legacy/` en place ; migration incrémentale paquet par
  paquet (la piste de l'ADR-0006 legacy).
- Conséquence : le noyau devient testable sans réseau ni base, et chaque adaptateur a une
  frontière explicite.
