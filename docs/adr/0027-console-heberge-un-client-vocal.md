# La Console héberge un Client pour la conversation vocale

## Status

accepted

Amende l'ADR-0003 et l'ADR-0024 : la Console n'est toujours **pas** un Client, mais elle
**héberge** un **Client Console** le temps d'une conversation vocale.

## Contexte

La Console sert déjà la conversation écrite par **Tours** (ADR-0024) : un message, une
Connexion Live ouverte pour sa durée, une réponse en texte et en audio, puis retour au Repos.
Elle ne capte pas le micro du navigateur, et l'ADR-0024 l'assume.

Une conversation vocale continue — parler, entendre, interrompre, laisser Athena enchaîner —
n'est pas une suite de Tours. Un Tour referme sa Connexion Live à chaque réponse : il n'y a
ni barge-in d'un tour sur l'autre, ni détection de fin de parole enchaînée, ni Session à
fermer, donc pas de `end_session` ni de timeout d'inactivité. Le noyau possède déjà cette
machine à états, mais elle suppose un **Client** : un `owner_client_id`, de l'audio transmis
en continu, un Client actif qui reçoit l'audio et les appels de Tools.

Or le navigateur ne peut pas être ce Client : il ne parle pas le Protocole partagé
(ADR-0002), et il ne doit pas détenir un **Jeton d'accès** `client` lisible par JavaScript
(ADR-0017). La Console, elle, est authentifiée par le seul **Ticket Console**.

Il fallait donc choisir : déformer le Tour, inventer une troisième unité, ou reconnaître
qu'une conversation vocale a besoin d'une Session — et donc d'un Client.

## Décision

### La Console héberge un Client Console

- Un adaptateur **en mémoire** du serveur tient le rôle de Client pendant la conversation. Il
  reçoit un `client_id`, un nom `Console (<navigateur>)` déduit côté serveur de l'en-tête
  `User-Agent`, et la plateforme `web`. Le nom ne vient jamais de la page.
- Il est routé comme les autres Clients : l'audio sortant lui est adressé par `client_id`, et
  sa disparition produit `ClientDisconnected`.
- Il est **éphémère** : il naît au démarrage de la conversation et meurt à sa fin. Hors
  conversation, l'instantané ne montre aucun Client Console.
- Il ne déclare aucun Tool client, ne reçoit aucun appel de Tool et n'a ni **Veille** ni
  **Wake word** : il ne capte que pendant sa Session.
- Il n'existe **aucun Jeton** associé : le Client Console est créé par le serveur pour une
  Console déjà authentifiée, il ne s'authentifie pas lui-même.

### Le navigateur reste hors du Protocole partagé

- L'authentification de la conversation vocale reste le **Ticket Console**.
- La voix utilise une **WebSocket dédiée** : binaire montant (PCM 16 bits mono 16 kHz,
  rééchantillonné dans le navigateur) et JSON descendant. L'ADR-0024 écartait une WebSocket
  pour le chat parce que le SSE suffisait à un flux unidirectionnel ; une conversation est
  bidirectionnelle et porte du binaire.
- Le navigateur ne manipule ni Protobuf, ni Jeton, ni `client_id`.

### Une Session ordinaire

- Le noyau ne change pas de machine à états : le Client Console dépose les mêmes entrées que
  les autres Clients, et la Session qui en résulte est une Session visible ordinaire.
- `end_session`, le timeout d'inactivité, le barge-in, le Journal et le Diagnostic
  s'appliquent sans règle nouvelle.
- Un seul Tour ou une seule Session à la fois. La Console **refuse** de démarrer si un autre
  Client tient déjà une Session, plutôt que de la préempter : voler la parole à un téléphone
  depuis un onglet web n'est pas un comportement de surface d'observation.
- Le noyau ignore l'audio reçu avant que la Connexion Live soit prête. La WebSocket expose
  donc un signal de disponibilité : le navigateur n'ouvre le micro qu'une fois celui-ci reçu.

### Transcription

- Les fragments de transcription d'entrée sont **accumulés dans l'adaptateur Gemini** et émis
  une seule fois à la fin du tour utilisateur : un énoncé produit un tour au Journal et un
  seul Rappel.
- La transcription de la réponse n'est disponible qu'en fin de tour (comportement de Gemini
  Live). Le fil affiche vos mots en direct, la réponse d'Athena d'un bloc — pas de sous-titre
  mot-à-mot.

### Interface

- Un bouton à côté de la barre de saisie démarre la conversation. La barre devient une **onde
  réactive au niveau réel du micro**, avec une croix pour terminer ; le fil reste visible
  au-dessus, sans plein écran.
- La **sourdine** disparaît pendant la conversation : on écoute Athena, la croix est le seul
  contrôle.
- La direction artistique existante s'applique : un seul accent, mouvement court ;
  `prefers-reduced-motion` remplace l'animation par un niveau statique.

### Durée de vie et arrière-plan

- La conversation continue lors d'un changement de zone dans la Console, et lorsque l'onglet
  est masqué sur un navigateur de bureau.
- Sur mobile, elle est **premier plan uniquement** : le système suspend le micro dès que la
  page passe en arrière-plan, et la conversation se termine avec un message qui l'explique.
  Aucune notification n'est demandée ; le web n'offre pas de service d'arrière-plan.
- Une coupure de liaison affiche un message explicite, tente **une** reconnexion automatique,
  puis propose de reprendre. Elle ne redemande jamais la permission micro.

## Pourquoi

- **La Session garde son sens.** Elle reste audio, avec un propriétaire, un timeout, un
  barge-in et une fin décidée par le modèle ; aucune de ces règles n'est réécrite.
- **Un seul cerveau.** La voix réutilise le prompt, la mémoire, les Tools et le Journal de la
  Session ; il n'y a pas de second assemblage de contexte.
- **Le navigateur ne détient aucun secret.** Le Ticket Console suffit, et le Client Console
  vit côté serveur.
- **Le Diagnostic dit la vérité.** Un Client Console visible et nommé vaut mieux qu'une
  Session sans propriétaire, qui laisserait croire à un appareil fantôme.

## Alternatives écartées

- **Étendre le Tour à l'audio entrant.** Chaque Tour referme sa Connexion Live : pas de
  conversation continue, pas de barge-in d'un tour sur l'autre, pas de `end_session` ni de
  timeout à fermer. C'est le cœur de ce qui était demandé.
- **Une troisième unité, « Conversation vocale ».** Ni Session ni Tour, il aurait fallu
  réimplémenter timeout, barge-in, visibilité et routage de l'audio, et maintenir deux cycles
  de vie en parallèle pour un vocabulaire qui ment sur ce qui se passe.
- **Le navigateur comme Client du Protocole.** Il faudrait un Jeton `client` lisible par
  JavaScript (ADR-0017) et du Protobuf dans le navigateur (ADR-0002).
- **Un Client Console persistant.** Il apparaîtrait dans les Clients sans rien faire et
  occuperait en permanence la place d'un Client actif.
- **Préempter la Session d'un autre appareil.** La préemption reste le comportement des
  Clients entre eux ; la Console refuse et l'explique.

## Conséquences

- Les définitions de `Console web` et le nouveau terme `Client Console` entrent au glossaire ;
  ADR-0003 et ADR-0024 sont amendés.
- L'accumulation des transcriptions d'entrée change aussi les Sessions desktop : un énoncé
  produit un tour au Journal, plus plusieurs fragments.
- L'avertissement « Session vocale en cours » de l'onglet Conversation doit ignorer le Client
  Console, sinon la Console se signale elle-même.
- Le micro exige un contexte sécurisé : `127.0.0.1` en développement, HTTPS en déploiement —
  déjà imposé par le cookie de la Console.
- Un second onglet qui demande une conversation est refusé par la règle de contention, sans
  identité distincte.
- Aucun appel de Tool client n'existe encore côté transport : le Client Console n'en reçoit
  pas, et cette décision n'en crée pas.
