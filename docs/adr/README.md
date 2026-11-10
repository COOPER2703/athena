# Index des ADR

## Numérotation

- Les ADR **du noyau et du système** vivent dans ce dossier et se numérotent au centre,
  à la suite, sur quatre chiffres (`0023-…`). C'est un compteur partagé : un ADR qui
  appartient à un plugin n'y a pas sa place.
- Les ADR **d'un plugin** vivent dans `plugins/<nom>/docs/adr/` et se numérotent
  **localement à partir de `0001`**. Deux plugins peuvent donc porter le même numéro :
  c'est voulu, chaque dossier est un contexte indépendant.
- Quand un ADR de plugin déménage du centre vers son dossier, il est **renuméroté** et la
  correspondance est ajoutée au tableau ci-dessous. Les citations de ces numéros ne
  vivent que dans des documents qui déménagent avec eux ; la renumérotation est donc bon
  marché.

## Correspondance ancien → nouveau

Table des ADR de plugin déplacés du centre vers `plugins/<nom>/docs/adr/`. Elle se
remplit à chaque déménagement (spec #147, tickets #149 à #154).

| Ancien ADR central | Nouvel ADR local | Ticket |
| ------------------ | ---------------- | ------ |

Aucune ligne pour l'instant : le premier déménagement la remplit.
