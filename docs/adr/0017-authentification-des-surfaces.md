# Authentification des surfaces publiques : Compte admin, Ticket Console et Jeton d'accès

## Status

accepted

Amende l'ADR-0003 : la Console n'est plus une surface « sans authentification en v1 » ; elle
exige un **Ticket Console** pour toute route d'API hors identité. Amende l'ADR-0011 : le
Webhook exige un **Jeton d'accès** de nature `source`, dont la Source fait autorité. Amende
l'ADR-0013 : la Session d'Annonce garde l'instruction de plugin mais **n'a plus les Tools du
noyau**. Ces trois ADR conservent leur texte d'origine et portent une note de renvoi.

Précise l'ADR-0016 : `GET /api/session` est une route d'**identité**, pas un instantané de
lecture du noyau — l'exception explicite à sa règle de route de lecture unique.

## Contexte

Athena expose trois surfaces : la **Console web** (SPA, API JSON et flux de Traces), le
**transport des Clients** (WebSocket, Protocole partagé) et le **Webhook** (poussée
d'Événements). Jusqu'ici aucune ne demandait de preuve d'identité, choix assumé tant qu'elles
restaient liées à `127.0.0.1` (ADR-0003, ADR-0011).

L'instance doit désormais être exposée sur Internet : la Console à la racine de
`athena.cooper-host.com`, le transport des Clients sur un hôte dédié. Dans cet état, sans
authentification :

- n'importe qui lit les **Traces**, donc le contenu des conversations ;
- n'importe qui injecte un **Événement** par `POST /api/events` et fait parler Athena ;
- n'importe qui se fait passer pour un **Client** et envoie de l'audio dans une **Session** ;
- un contenu externe peut être interprété comme une instruction par le juge puis par la
  **Session d'Annonce**, qui dispose des outils serveur.

Le problème n'est pas « ajouter un mot de passe » : il faut donner à chaque surface une preuve
d'identité **adaptée à son type de client**, sans casser le Protocole partagé (ADR-0002) et
sans transformer Athena en plateforme multi-tenant.

## Décision

### Authentification applicative, embarquée dans le serveur

Les trois surfaces sont exposées sur Internet avec une authentification **applicative**, dans
le serveur lui-même : aucun proxy d'authentification externe, aucun service d'identité tiers.
Le serveur possède les Comptes, les Tickets Console et les Jetons d'accès, et chaque
adaptateur reçoit le même service d'auth par son constructeur.

### Compte et Code d'installation

- Un **Compte** unique : nom d'utilisateur et mot de passe haché en **Argon2id** (paramètres
  par défaut de la bibliothèque, calcul déporté hors de la boucle). Aucune inscription
  publique, aucun rôle, aucune création de Compte en v1.
- Tant qu'aucun Compte n'existe, le serveur génère au démarrage un **Code d'installation** à
  usage unique et l'écrit dans le journal (`logs/server.log`, source `[auth]`). Le code vit
  en mémoire seulement : il n'est écrit qu'au démarrage, jamais réaffiché en cours de
  processus, et un redémarrage sans Compte en produit un nouveau.
- La Console affiche un écran d'installation tant que `setup_required` est vrai ; le premier
  visiteur fournit le code, un nom d'utilisateur et un mot de passe (longueur minimale imposée
  côté serveur), et devient l'unique admin. Une fois le Compte créé, l'installation est
  fermée pour de bon : `409 already_installed`, et le code encore en mémoire ne peut plus
  servir.
- Hors routes publiques d'identité, une instance non installée répond `409 setup_required` à
  toute requête, avant même de demander un Ticket.
- La connexion échoue avec un `401` générique, sans distinguer un utilisateur inconnu d'un
  mot de passe faux. Après cinq échecs, le nom d'utilisateur est bloqué quinze minutes ; le
  blocage se lève seul et un succès remet le compteur à zéro.

### Deux mécanismes de credential, selon le type de client

| Type de client | Credential | Preuve |
| --- | --- | --- |
| Humain, navigateur | **Ticket Console** | Cookie `athena_ticket` : `HttpOnly; Secure; SameSite=Strict`, `Path=/`, 30 jours glissants |
| Machine : Client desktop, ESP32, source Webhook | **Jeton d'accès** | `Authorization: Bearer ath_…` |

**Ticket Console** — état authentifié d'un navigateur. Le secret est aléatoire (256 bits),
seule son empreinte SHA-256 est stockée. Il est révocable immédiatement (déconnexion,
« déconnecter partout ») et son expiration n'est repoussée qu'au plus une fois par heure, pour
éviter une écriture par requête. Il authentifie aussi le WebSocket du flux de Traces. Le front
ne le lit jamais : il est `HttpOnly` et n'est jamais conservé dans le stockage local.

**Jeton d'accès** — credential d'une machine : nommé, révocable individuellement, sans
expiration, avec une date de dernière utilisation. Son secret est préfixé `ath_` pour qu'une
fuite accidentelle soit détectable par recherche de chaîne ; son empreinte SHA-256 est seule
stockée et il n'est affiché qu'à la création. Il porte une **nature** : `client` pour le
transport des Clients, `source` pour le Webhook. Un Jeton ne vaut jamais sur l'autre surface.
La dernière utilisation est écrite au plus une fois par minute et par Jeton.

### Interface et stockage

- Le port **`AuthService`** vit dans `server/core/ports.py` : aucune I/O, aucun SQL, aucune
  dépendance à Argon2 ni à HTTP. Les adaptateurs Console, transport et Webhook ne connaissent
  que cette interface.
- L'implémentation **`SqliteAuthStore`** vit dans `server/adapters/auth/` : SQLite, Argon2id,
  génération et validation des secrets, compteur de tentatives en mémoire, horloge injectée.
- Les données vivent dans une base **séparée** de la mémoire : `auth.db` par défaut,
  surchargeable par `ATHENA_AUTH_DB`. Trois tables (Comptes, Tickets Console, Jetons
  d'accès), créées par `CREATE TABLE IF NOT EXISTS`. Aucune infrastructure de migration n'est
  introduite : les évolutions devront rester additives.
- `build_app` construit **un seul** store et l'injecte aux trois adaptateurs ainsi qu'à
  `App` : une révocation prononcée depuis la Console vaut pour le transport et le Webhook
  sans redémarrage.
- Les constructeurs d'adaptateurs gardent `auth=None` comme défaut : c'est le seam des tests
  unitaires de surface, pas un mode de production. `build_app` injecte toujours le store
  partagé ; rendre l'auth obligatoire au constructeur est un suivi possible, pas ce lot.

### Console : middlewares, liste blanche et refus

Ordre imposé : **installation**, puis **Ticket Console**, puis contrôle d'**`Origin`** pour
les méthodes mutantes (`POST`, `PUT`, `PATCH`, `DELETE`).

- Liste blanche sans identité : la page HTML et ses assets, `/healthz`, `/api/session`,
  `/api/setup` et `/api/login`.
- Toute autre route d'API répond `401` en JSON, **y compris une route inconnue** : un client
  d'API ne reçoit jamais une page HTML à la place d'un refus d'identité.
- Toute route hors API continue de servir la SPA : c'est elle qui affiche l'écran de connexion.
- Le WebSocket du flux hérite du Ticket par le cookie et le vérifie **avant** l'upgrade ; un
  client sans Ticket est refusé avant l'ouverture de la socket applicative. Le Ticket est
  ensuite revérifié pendant que le flux est ouvert : une révocation ferme le WebSocket au
  lieu d'attendre la reconnexion.
- `Origin` est comparé strictement à l'hôte de la requête (le schéma est ignoré pour que TLS
  terminé par un proxy ne casse pas le contrôle) ; une origine étrangère **ou absente** est
  refusée `403`. Un navigateur envoie toujours `Origin` sur une mutation : son absence est le
  cas d'un client forgé.
- `GET /healthz` est public, répond une charge fixe (`{"status": "ok"}`) et ne lit ni
  l'instantané, ni le Tampon de traces, ni aucun secret.

### Transport des Clients

- Le handshake lit `Authorization: Bearer`, valide un Jeton de nature **`client`** avant tout
  traitement de trame, et refuse par `401` avec `WWW-Authenticate: Bearer` sinon.
- Le Protocole partagé n'est **pas** modifié : aucun champ ajouté à `Register` (ADR-0002).
- La révocation prend effet au handshake suivant, sans redémarrage.
- Le client desktop lit son Jeton dans `ATHENA_ACCESS_TOKEN`, l'envoie au handshake et échoue
  avec un message explicite s'il manque ou est refusé.

### Webhook

- Le Jeton de nature **`source`** identifie la Source : c'est lui qui fait autorité.
- Le champ `source` du corps reste accepté pour compatibilité ; s'il contredit la Source du
  Jeton, la requête est refusée `403`. Sans Jeton valide, `401`.
- Les limites de taille, la validation du corps et les Traces existantes ne changent pas.

### Session d'Annonce sans outil et données non fiables

- La Connexion Live ouverte par une **Annonce** déclare une liste de Tools **vide**. Une
  Session demandée par un Client conserve les siens : les deux branches ne partagent plus la
  même fabrique d'outils.
- Les prompts du juge et de l'Annonce encadrent le contenu externe, le déclarent **donnée non
  fiable** et interdisent d'en suivre les instructions. Le juge reçoit les faits entre
  délimiteurs ; une annonce dont le message est vide est une **faute fermée** (`failed`), le
  contenu brut n'est jamais prononcé.
- L'instruction d'Annonce conserve l'instruction de plugin mais ne déclare **aucun** Tool :
  l'asymétrie est intentionnelle, ce n'est pas un oubli à « corriger ».
- L'exécution d'un appel d'outil (`_on_llm_tool_call`) accepte encore tout nom présent dans le
  registre : la liste vide à l'ouverture est le garde-fou **structurel**, une politique par
  Connexion serait la ceinture suivante (non implémentée).

### Configuration

Nouvelles variables, toutes avec un défaut sûr et **aucun secret par défaut** :

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `ATHENA_AUTH_DB` | `auth.db` | base d'auth séparée de la mémoire |
| `ATHENA_CONSOLE_TICKET_DAYS` | `30` | durée glissante du Ticket Console |
| `ATHENA_CONSOLE_TICKET_REFRESH_SECONDS` | `3600` | fréquence maximale du rafraîchissement |
| `ATHENA_AUTH_PASSWORD_MIN_LENGTH` | `12` | longueur minimale du mot de passe |
| `ATHENA_AUTH_LOGIN_MAX_ATTEMPTS` | `5` | échecs avant blocage |
| `ATHENA_AUTH_LOGIN_BLOCK_MINUTES` | `15` | durée du blocage |
| `ATHENA_AUTH_SETUP_MAX_ATTEMPTS` | `5` | tentatives d'installation avant blocage |
| `ATHENA_ACCESS_TOKEN` | — (client) | Jeton `client` lu par le client desktop |

Les surfaces gardent leurs variables existantes : `ATHENA_CONSOLE_ENABLED`,
`ATHENA_CONSOLE_HOST`, `ATHENA_CONSOLE_PORT`, `ATHENA_CONSOLE_STATIC_DIR`,
`ATHENA_CONSOLE_TRACE_BUFFER`, `ATHENA_WEBHOOK_ENABLED`, `ATHENA_WEBHOOK_HOST`,
`ATHENA_WEBHOOK_PORT`, et `WS_HOST` / `WS_PORT` pour le transport.

## Pourquoi

- **Un credential par type de client.** Un navigateur peut garder un cookie `HttpOnly` ; une
  machine ne le peut pas et présente un porteur explicite. Traiter les deux avec le même
  mécanisme forcerait soit un secret lisible par JavaScript, soit un mot de passe stocké sur
  chaque appareil.
- **Le Protocole reste figé.** L'identité d'un Client est une affaire de handshake HTTP, pas
  de message applicatif : aucun codegen client n'est touché et le contrat des messages reste
  identique. Les clients existants doivent en revanche présenter un Jeton — c'est le
  changement d'exploitation, pas un changement de Protocole.
- **Une révocation réelle.** Les secrets sont vérifiés en base à chaque requête ou handshake,
  pas seulement signés : « déconnecter partout » et la révocation d'un Jeton prennent effet
  sans redémarrage.
- **La base d'auth est séparée.** Sauvegarder ou restaurer la mémoire ne touche pas aux
  credentials, et une fuite de la mémoire ne livre pas les mots de passe.
- **La mesure anti-injection est structurelle.** Une Session d'Annonce sans aucun outil ne
  peut pas agir, même si le contenu externe convainc le modèle ; le durcissement des prompts
  est une seconde couche, pas la seule.
- **L'auth embarquée reste une affaire d'application.** Elle protège les trois surfaces avec
  la même vérité (le store), sans dépendre d'un tiers ni d'un déploiement particulier.

## Alternatives écartées

- **`basicauth` de Traefik.** Un mot de passe statique par surface, identique pour tous les
  utilisateurs d'une surface : aucune révocation individuelle, aucune identité de machine,
  aucun cycle de vie de Compte, aucune distinction `client` / `source`, et un secret partagé
  qui ne dit pas quel appareil l'utilise. C'est un verrou de transport, pas une
  authentification des surfaces.
- **Cloudflare Access / Zero Trust.** Dépendance externe, décision d'infrastructure hors du
  serveur. Cela ne protège ni le transport des Clients ni le Webhook sur un hôte dédié si
  ceux-ci ne passent pas par le même bord, et cela transforme l'authentification en problème
  de déploiement alors que la spec veut une auth embarquée, vérifiable par les tests du
  serveur.
- **Un champ de Jeton dans le Protocole partagé.** Rouvrir l'ADR-0002, changer le schéma
  Protobuf, régénérer tous les clients et faire porter à chaque trame une préoccupation de
  handshake. L'en-tête HTTP `Authorization` existe déjà et ne demande aucun changement de
  contrat.

## Conséquences

- **Base et dépendance.** Une base `auth.db` séparée, trois tables additives, et une nouvelle
  dépendance Python (`argon2-cffi`). Aucune migration : toute évolution devra être additive.
- **Cookie `Secure` et HTTP.** Le Ticket n'est posé qu'en `Secure` : il faut HTTPS ou un
  contexte de boucle locale. En HTTP simple sur une adresse du LAN, le navigateur ignore
  silencieusement le cookie et la Console renvoie l'écran de connexion en boucle. Il n'existe
  pas de dérogation de configuration dans ce lot ; ne pas retirer `Secure` sans rouvrir cette
  décision.
- **`Origin` et proxy inversé.** Le contrôle compare l'`Origin` à `request.host`. Un proxy qui
  réécrit l'en-tête `Host` doit donc transmettre l'hôte d'origine, sinon des mutations
  parfaitement légitimes reçoivent `403`.
- **Limitation par nom d'utilisateur, pas par IP.** Derrière un proxy puis un CDN, l'IP est
  celle du proxy et `X-Forwarded-For` serait contournable par en-tête forgé. Le choix assume
  qu'un attaquant peut bloquer temporairement l'admin en épuisant ses tentatives (déni de
  service borné à quinze minutes). Les noms d'utilisateur inconnus ont chacun leur propre
  compteur, donc la limite ne bloque pas l'instance entière.
- **Code d'installation lisible par l'hôte.** Quiconque lit le journal peut revendiquer
  l'instance tant qu'aucun Compte n'existe. C'est accepté : un accès à l'hôte donne déjà un
  accès équivalent à root sur les conteneurs. La portée est strictement limitée — une fois,
  et seulement avant le premier Compte.
- **Jugement fermé sur message vide.** `EventJudgmentFailed` peut désormais porter
  `reason="missing_message"` : un verdict `announce=true` sans phrase est traité comme un
  échec, jamais comme une invitation à prononcer le contenu externe brut.
- **`GET /api/session` n'est pas un instantané.** C'est une route d'identité et
  d'installation ; elle ne lit pas le noyau. C'est l'exception explicite à la règle de
  l'instantané de lecture unique de l'ADR-0016. Toute lecture d'état continue de passer par
  `App.snapshot()`.
- **Session d'Annonce durable, sans outils.** Si l'utilisateur prend la parole pendant une
  Session d'annonce et demande une action nécessitant un outil, Athena répond sans agir.
  Rouvrir la Connexion avec des outils au premier tour utilisateur est hors périmètre : c'est
  le prix de la seule mesure anti-injection structurelle.
- **Asymétrie des prompts d'Annonce.** L'instruction de plugin est conservée alors qu'aucun
  Tool n'est déclaré. C'est volontaire : l'instruction décrit l'usage des Tools, la
  déclaration vide est la garde. Ne pas « réparer » cette asymétrie sans décision contraire.
- **Garde-fou structurel, pas politique par Connexion.** La liste vide à l'ouverture empêche
  Gemini d'appeler un Tool ; un appel halluciné portant un nom du registre serait encore
  exécuté. Une politique par Connexion est la ceinture suivante, notée mais non implémentée.
- **Seam `auth=None`.** Le défaut garde les tests unitaires d'adaptateur légers et n'ouvre
  aucune surface en production, puisque `build_app` injecte toujours le store partagé. Rendre
  l'auth obligatoire au constructeur serait un suivi ; le test d'intégration de `build_app`
  est la preuve que la production est fermée.
- **Un seul Compte, une seule Athena.** Pas de rôles, pas de cloisonnement de la mémoire, des
  Sessions ni des Traces par Compte. Le schéma prépare des Comptes supplémentaires, l'interface
  ne les expose pas.
- **Documentation et exploitation.** Le glossaire gagne Compte, Ticket Console, Jeton d'accès
  et Code d'installation ; `AGENTS.md` et les smoke tests portent la procédure d'installation
  et les en-têtes `Authorization`. Aucun secret n'a de valeur par défaut ; le Code
  d'installation et les secrets de Jeton ne sont affichés qu'une fois.
