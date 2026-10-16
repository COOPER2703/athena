# Accès Webhook : une surface HTTP locale, `aiohttp`, branchée sur `push_event`

## Status

accepted

## Contexte

Le Canal d'événements (ADR-0007 / ADR-0010) est complet côté noyau — File plafonnée et
éphémère, fenêtre de regroupement, dédup, préemption, Journal en rôle `event` — mais **aucune
Source ne peut y pousser un Événement** : `App.push_event` existe, documenté comme point
d'entrée « adaptateur Webhook à venir », sans appelant. ADR-0007 avait explicitement différé
le moyen d'accès (« on livre le port noyau ; le moyen d'accès viendra ensuite »). ADR-0003
prévoit une console web servie par le serveur, mais elle n'existe pas encore.

## Décision

- Un adaptateur **Webhook** (`server/adapters/webhook/`), seul moyen d'accès HTTP du serveur à
  ce jour, câblé dans la racine de composition et paramétré par `WebhookConfig`.
- Transport **`aiohttp`**, sur la **boucle asyncio unique** (ADR-0005) — comme le transport
  WebSocket l'est par `websockets`.
- Le noyau n'est **pas traversé** par l'adaptateur : la route appelle la couture injectée
  `App.push_event`, qui dépose dans le **Canal d'événements unique** (ADR-0010). Aucune file
  propre, aucune seconde machine à états.
- Contrat : `POST /events`, corps JSON `{"source", "content"}`, **Source fournie par
  l'appelant** (défaut `webhook`), **un seul Événement par requête**. `202 Accepted` sur
  succès ; `400` (JSON invalide ou champ vide), `404` (chemin inconnu), `405` (mauvaise
  méthode), `413` (contenu au-delà du plafond du noyau), `415` (Content-Type non JSON).
- **Aucune authentification**, lié à **`127.0.0.1`** par défaut, port dédié **8766**,
  **activé par défaut**. L'exposition au LAN (jeton partagé) est renvoyée à un lot ultérieur.

## Pourquoi

- `push_event` est déjà la couture noyau documentée pour cet usage : l'adaptateur reste un
  transport, sans dupliquer le gate du Repos ni la File d'attente.
- `aiohttp` plutôt qu'un serveur HTTP écrit à la main (un parseur à maintenir pour une route)
  ou que le détournement de `websockets` (conçu pour le handshake, pas pour servir une API) :
  une dépendance mature, async-natif, sur la boucle unique.
- `202` dit la vérité : l'Événement est **déposé**, pas jugé — le jugement suit la fenêtre de
  regroupement de 5 s (ADR-0007). L'appelant n'attend pas de verdict.
- **Localhost sans auth par défaut** : la surface n'est pas ouverte au réseau tant qu'on ne le
  demande pas ; le jeton viendra avec l'exposition LAN.

## Alternatives écartées

- **Stdlib asyncio + parsing HTTP maison** : un parseur à maintenir pour une seule route.
- **Détourner `websockets` (`process_request`)** : il sert le handshake, pas une API.
- **Jeton obligatoire dès ce lot** : inutile sur localhost ; repoussé avec l'exposition LAN.
- **Lot d'Événements par requête** : le regroupement est déjà le métier du noyau (ADR-0007).

## Conséquences

- `aiohttp` rejoint `websockets` dans les dépendances du serveur ; le **noyau reste pur** (le
  garde-fou `test_purity` ne couvre que `server/core/`).
- `App.run` démarre et arrête l'adaptateur Webhook en plus du transport, de Gemini et de
  l'horloge.
- Une Source externe peut désormais pousser un fait sans connaître la politique du noyau ;
  `App.push_event` cesse d'être un point d'entrée sans appelant.
- Un fait poussé alors qu'aucun Client n'est enregistré est tout de même jugé, mais l'Annonce
  n'a aucune cible : c'est un **prérequis de test**, pas un défaut.
