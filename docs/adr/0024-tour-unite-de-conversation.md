# Le Tour, unité de conversation, et la Console comme surface de conversation

## Status

accepted

Précise l'ADR-0003 : la Console devient la surface de conversation écrite promise, sans
devenir un Client. Amende l'ADR-0016 : le fil de conversation est une lecture du **Journal**,
pas un nouvel état du noyau.

## Contexte

La Console web doit permettre d'échanger avec Athena par écrit, en plus de la configuration
d'une instance. Or le noyau n'a qu'une machine à états de **Session**, dont toutes les règles
supposent un **Client** : `owner_client_id`, `Client actif`, `ACTIVE`, audio transmis en
continu, Wake word pour démarrer. Une surface web authentifiée par **Ticket Console** n'est
pas un Client (ADR-0003) : elle ne parle pas le Protocole partagé et ne transmet pas d'audio.

Ajouter du texte impose donc de choisir entre déformer la Session ou introduire une unité plus
petite. La Session n'est pas une bonne unité pour un message écrit : elle suppose un propriétaire
matériel, un timeout, un barge-in et une Connexion Live maintenue, alors qu'un message écrit
arrive, reçoit sa réponse et se termine.

Le noyau doit par ailleurs rester un seul cerveau : même prompt système, même mémoire, mêmes
Tools, même Journal. Une seconde voie qui réinvente le prompt et la mémoire produirait deux
Athena qui ne se souviennent pas l'une de l'autre.

## Décision

### Le Tour est l'unité d'interaction

Un **Tour** est : une entrée (texte ou audio), le contexte assemblé (instruction système,
Faits épinglés, Rappel, Tools du moment), une **Connexion Live** ouverte pour sa durée, et une
**destination** qui reçoit le texte, l'audio et les appels de Tools.

- Un **Tour texte** est isolé : il ouvre sa Connexion Live, envoie l'entrée, relaie la réponse,
  puis referme la Connexion Live. Le noyau revient au **Repos** entre deux Tours.
- Une **Session** enchaîne plusieurs Tours sur une seule Connexion Live, avec un Client
  propriétaire.
- Un Tour texte ne crée **aucune Session** : `owner_client_id` reste vide, aucun Client ne
  passe en `ACTIVE`, et l'instantané ne montre pas de Session.
- Les Tools d'un Tour sont ceux d'une Session demandée par un Client (noyau et Plugins),
  jamais la liste vide d'une Annonce.
- Un Tour écrit dans le **Journal** comme une Session (tour utilisateur, tour assistant) : il
  alimente la mémoire et le fil de la Conversation.

### La Console est une surface de conversation, pas un Client

- L'adaptateur Console reçoit du noyau une **destination de Tour**, comme il reçoit déjà la
  callable de l'instantané (ADR-0016). Il ne voit ni le Coordinator, ni `SessionState`.
- Le Tour part de la Console en HTTP et sa réponse arrive en `text/event-stream` : événements
  `text`, `audio`, `tool`, `done`, `error`, `busy`. Fermer la réponse annule le Tour.
- Le **Protocole partagé** ne change pas. L'extension au texte pour Android et iOS est une
  décision séparée ; le concept de Tour, lui, est déjà dans le noyau.
- Le fil de la Conversation est une **lecture paginée du Journal**, pas un état conservé par
  le noyau : il montre les Tours texte, les transcriptions des Sessions vocales et les
  Annonces, marqués par leur origine, avec un rappel de la fenêtre de rétention.

### Un seul Tour à la fois

- Un Tour demandé pendant une Session ou un autre Tour est refusé avec un motif lisible ; une
  Session demandée pendant un Tour l'est aussi.
- Aucune mise en file, aucune préemption : préempter tuerait une conversation en cours sans le
  dire, et la file produirait une réponse différée incompréhensible.

### Audio et sourdine

- La réponse d'un Tour est toujours affichée en texte, et prononcée quand l'audio est activé.
- La sourdine est **active par défaut** : elle évite de surprendre et ne dépend pas de
  l'autorisation de lecture automatique du navigateur.

## Pourquoi

- **La Session garde son sens.** Elle reste audio, portée par un Client, avec ses règles de
  Wake word, de barge-in et de fin. Y injecter du texte obligerait à qualifier chacune de ces
  règles et à inventer un « propriétaire » qui n'est pas un appareil.
- **Le Wake word reste disponible.** Entre deux Tours, le noyau est au Repos : la voix n'est
  jamais bloquée par un chat ouvert.
- **Un seul cerveau.** Le Tour réutilise le prompt, la mémoire, les Tools et le Journal de la
  Session ; il n'y a pas de second assemblage de contexte à maintenir en parallèle.
- **Le navigateur ne détient aucun secret.** Authentifié par le seul cookie `HttpOnly`, il ne
  peut pas porter un Jeton d'accès lisible par JavaScript (ADR-0017).
- **Un objet réutilisable.** Les Annonces ouvrent déjà une Connexion Live sans Session ; le
  Tour donne un nom à ce motif et servira aux clients mobiles.

## Alternatives écartées

- **Une Session texte.** Réutiliser la machine à états avec la Console comme propriétaire :
  `owner_client_id` et le glossaire mentent, il faut régler timeout, barge-in et contention
  avec la voix, pour un gain de latence qui ne justifie pas la dette de modèle.
- **Un second modèle texte, hors Live.** Prompt, mémoire et Tools à reconstruire : deux
  cerveaux, deux historiques, deux vérités.
- **Le navigateur comme Client du Protocole.** Il faudrait un Jeton `client` lisible par
  JavaScript, interdit par l'ADR-0017, ou un proxy qui rejoue le credential.
- **Une WebSocket dédiée au chat.** Requête/réponse unidirectionnelle : le SSE donne le
  streaming, l'annulation par fermeture et la traversée de proxy sans logique de reconnexion
  ni de tramage binaire.

## Conséquences

- Nouveaux événements et commandes du noyau pour un Tour, et un port de **destination de Tour**
  fourni par la racine de composition.
- Route de lecture du Journal : c'est une lecture dynamique paginée, pas une extension de
  l'instantané. L'ADR-0016 est amendé en conséquence.
- Un Tour occupe le noyau (il n'est pas au Repos) : les Événements attendent, comme pendant une
  Session. Le refus explicite est la seule politique.
- Le flux SSE exige la désactivation du buffering côté proxy.
- L'interface doit offrir l'annulation d'un Tour et expliquer le refus « Athena est occupée ».
- Le refus symétrique côté Client (« occupé ») doit être lisible par le daemon, sans changer
  le Protocole.
