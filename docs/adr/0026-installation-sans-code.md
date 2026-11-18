# Installation sans code : le premier visiteur devient propriétaire

## Status

accepted

Amende l'ADR-0017 : le **Code d'installation** disparaît. L'Installation se termine quand
l'unique Compte existe et qu'un **Secret** Gemini utilisable est enregistré.

## Contexte

L'ADR-0017 a protégé la création du premier Compte par un code à usage unique, écrit dans le
journal du serveur tant qu'aucun Compte n'existe. Ce choix était cohérent avec une instance
exposée sur Internet et un opérateur qui lit les journaux.

Deux faits le contredisent aujourd'hui :

- le serveur refuse de démarrer sans `GEMINI_API_KEY`, donc une installation neuve meurt avant
  d'afficher quoi que ce soit ;
- l'installation visée est une commande unique, suivie d'un navigateur. Demander d'aller lire
  `logs/server.log` pour recopier un code est précisément l'étape qui transforme un
  non-initié en opérateur.

## Décision

- **Aucun code n'est demandé.** Le premier visiteur de l'écran d'installation crée l'unique
  Compte ; une fois créé, l'installation est fermée pour de bon et répond comme avant.
- **L'Installation est un état de l'instance**, pas seulement de l'authentification : elle
  couvre le Compte et un Secret Gemini utilisable.
- Le serveur **démarre sans clé Gemini**, en mode Installation : seuls l'écran d'installation,
  les Réglages et le Diagnostic répondent ; le transport des Clients, le Webhook et Gemini
  restent fermés, et la Conversation est désactivée côté serveur et côté interface.
- L'étape de la clé est **passable**. Tant qu'elle manque, un rappel permanent et non masquable
  subsiste et mène au réglage manquant ; Athena ne peut ni parler ni être jointe.
- Le parcours est : Compte, puis clé Gemini (validée), puis voix et profil (pré-remplis), puis
  récapitulatif de ce qui reste à faire.
- **La fin de l'Installation s'applique sans nouvelle action** : enregistrer la première clé
  Gemini utilisable ouvre à chaud, dans le processus vivant, le transport des Clients, le
  Webhook, Gemini, le juge et la Consolidation. `restart_required` retombe à faux et la
  Conversation répond immédiatement. Seule une clé **remplacée** après installation attend un
  redémarrage : la Connexion Live garde celle chargée au démarrage, et la Conversation reste
  utilisable avec l'ancienne jusque-là.

## Pourquoi

- **Le code ne protégeait qu'une fenêtre.** Tant que le Compte n'existe pas, quiconque atteint
  l'écran peut le revendiquer ; la seule vraie protection est de ne pas exposer l'instance
  avant la fin de l'Installation.
- **Le code coûtait l'expérience entière.** Il exigeait de lire un fichier serveur, ce qui
  annule la promesse d'une installation en une commande et d'une Console utilisable sans
  terminal.
- **La surface reste fermée pendant l'Installation.** Même revendiquée par un tiers, une
  instance non installée n'ouvre ni transport, ni Webhook, ni Gemini : le pire cas est une
  instance configurée par un inconnu, pas une instance qui parle ou qui agit.
- **Le démarrage sans clé est de toute façon nécessaire.** Sans lui, il n'y a pas d'écran
  d'installation du tout.

## Alternatives écartées

- **Garder le code et l'imprimer depuis `install.sh`.** Ça marche pour l'installation scriptée,
  pas pour l'installation Docker ou manuelle, et ça garde une étape serveur dans un parcours
  navigateur.
- **N'accepter l'installation que depuis la boucle locale.** Derrière un proxy inversé, la
  requête paraît locale : le garde-fou ne protège de rien.
- **Une fenêtre temporelle après le démarrage.** Une fenêtre trop courte bloque une installation
  interrompue ; une fenêtre trop longue ne protège plus. Elle ajoute un état sans bénéfice.

## Conséquences

- **Risque accepté** : le premier visiteur devient propriétaire. La mitigation est
  opérationnelle, pas logicielle : terminer l'Installation avant d'exposer le service, ce que
  `install.sh` doit recommander explicitement.
- `setup_required` devient un état d'Installation ; les routes publiques d'identité doivent
  distinguer « pas de Compte » de « Compte sans clé utilisable ».
- La configuration doit accepter une clé Gemini absente au démarrage, ce qui touche la racine
  de composition et les adaptateurs qui supposaient un secret présent.
- La racine de composition doit pouvoir construire et démarrer les surfaces d'exécution **après**
  le démarrage, sur le même processus et la même boucle : la fabrique de runtime et l'échange
  d'adaptateurs vivent dans `server/app/root.py`, jamais dans l'adaptateur Console.
- Le glossaire perd **Code d'installation** et gagne **Installation** et **Secret**.
- Un déploiement qui laisse une instance non installée exposée est un incident d'exploitation,
  pas un défaut à rattraper dans le code.
