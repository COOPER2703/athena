# Protocole : Protobuf pour les clients audio, JSON pour la console

Le fil client audio ↔ serveur (desktop, Android, demain iOS) est défini par un schéma
**Protobuf** language-neutral, généré pour chaque langage : contrat binaire fort, champs
numérotés stables, compatible dans le temps, compact pour l'audio. La **console web** et
les futures apps mobiles côté produit consomment une **API JSON** distincte (REST pour les
commandes, WebSocket pour le temps réel). Les deux fils coexistent : chacun a son usage.

## Considérations

- Options rejetées : tout en JSON sur le fil natif (pas de schéma imposé, verbeux) ; tout
  en Protobuf côté web (lourd pour un navigateur).
- Conséquence : deux codecs, un par fil, et l'API JSON devient le contrat réutilisable par
  les clients mobiles.
