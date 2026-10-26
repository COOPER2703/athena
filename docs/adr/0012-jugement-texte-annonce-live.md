# Jugement par modèle texte, puis Annonce par le Live

## Status

accepted

Amende l'ADR-0007 : le verdict de jugement ne vient plus du modèle Live vocal mais d'un
modèle **texte** hors Session.

Amendé par l'ADR-0013 : la Connexion d'Annonce devient une Session ordinaire — prompt système,
Activation mémoire, Tools — et reçoit aussi les faits bruts de l'Événement.

## Contexte

L'ADR-0007 faisait reposer le verdict sur le **silence** de la Connexion Live : le lot
était transmis comme contenu de tour, et si le LLM parlait la Connexion devenait une
Annonce, sinon un timeout dédié la refermait. Le noyau concluait « le LLM a parlé » au
premier `LlmAudio` reçu.

En pratique, le modèle Live **ne sait pas se taire** : un tour utilisateur complet
(`turn_complete=True`) le pousse toujours à générer de l'audio, quoi que dise l'instruction
système. Mesuré sur `gemini-3.1-flash-live-preview` :

- une instruction système « Tu ne parles JAMAIS » produit quand même ~1 s d'audio ;
- un fait banal (« La température du salon est de 19 degrés ») est **récité à l'oral** 3 fois
  sur 3, sans aucun filtrage ;
- un modèle **texte** (`gemini-3.8-flash`, sortie structurée) sépare proprement banal
  (`announce=false`) et important (`announce=true`), mais son quota gratuit (20 requêtes/jour)
  et sa latence (~30-50 s) le rendent inutilisable comme juge par défaut.

Conséquence : la moindre audition ouvrait une Session automatique — « le jugement ne se fait
pas, la session se lance à chaque fois ».

## Décision

- À l'échéance de la fenêtre de regroupement, le noyau juge le lot par un **modèle texte**
  non-live (`generate_content`), via le port `EventJudge`. Le juge rend un verdict structuré
  `{announce, message}` : `message` est la phrase à prononcer, vide si `announce` est faux.
- Le noyau n'ouvre une **Connexion Live** que si le verdict annonce. Cette Connexion
  (« Connexion d'Annonce ») reçoit une instruction d'annonce dédiée (`announce-prompt.txt`),
  **sans Tools**, et le message du juge ; son premier son en fait une Session automatique
  annoncée au Client de référence.
- Un fait **tu** est simplement **consommé** : il n'ouvre aucune Connexion et n'entre pas au
  Journal (inchangé depuis l'ADR-0007).
- Le **timeout d'annonce** (`ANNOUNCE_TIMEOUT`, plus court que l'inactivité de Session) reste
  un **filet de sécurité** : si la Connexion d'Annonce ne produit aucun son, elle est
  refermée sans qu'aucune Session n'apparaisse.
- Une panne de jugement (réseau, réponse non-JSON) est une **faute fermée** : `announce=false`,
  on ne dérange pas l'utilisateur dans le doute. Le juge ne lève jamais. Un pic transitoire du
  fournisseur (503/429) est d'abord **retenté** (trois tentatives, backoff court) avant la
  faute fermée, sinon une Session automatique serait silencieusement perdue.
- Le modèle de jugement est configurable (`GEMINI_JUDGE_MODEL`, défaut
  `gemini-3.1-flash-lite`), distinct du modèle Live. Un modèle Flash-Lite est retenu par
  défaut : il tient le quota gratuit et juge en quelques secondes, là où `gemini-3.8-flash`
  épuisait son quota (20 requêtes/jour) et rendait des verdicts `failed` (faute fermée),
  donc plus aucune Session automatique. Le prompt de jugement reste versionné
  (`judge-prompt.txt`) et distingue explicitement les messages personnels (invitation,
  rendez-vous, question) du bruit ambiant (domotique, spam, bavardage de groupe).

Le reste de l'ADR-0007 est inchangé : jugement au Repos seul, fenêtre de regroupement
paresseuse, File d'attente plafonnée et éphémère, « l'utilisateur gagne toujours », une seule
Connexion Live à la fois.

## Pourquoi

- Le verdict est **séparable** du support audio : un classifieur texte juge, le Live se
  contente de parler. Le modèle Live n'est plus sollicité pour une décision qu'il ne sait pas
  rendre.
- Un fait banal n'ouvre plus ni Connexion Live ni Session : coût et dérangement proportionnels
  à l'importance réelle.
- La faute fermée du juge évite qu'une panne réseau se traduise par une salve d'annonces.

## Alternatives écartées

- **Renforcer le prompt du modèle Live** : testé, il récite quand même le fait. Le problème
  n'est pas le libellé mais la nature du modèle Live.
- **Verdict par appel de Tool du Live** : il faut ignorer l'audio émis pendant le jugement et
  gérer un aller-retour supplémentaire ; le chemin est plus retors et le modèle peut toujours
  produire du son parasite.
- **Détecter le silence par un signal de fin de tour** : l'ADR-0007 l'avait déjà écarté, et le
  modèle produit de l'audio de toute façon.

## Conséquences

- Le noyau dépend d'un nouveau port async `EventJudge` ; l'appel réseau vit dans l'adaptateur
  (`GeminiEventJudge`, `asyncio.to_thread`), jamais dans le noyau pur.
- La commande de transmission au Live est renommée `SendAnnouncementToLlm` (elle porte le
  message de l'Annonce, plus un lot à juger).
- Le glossaire (« Connexion Live ouverte pour juger » → ouverte pour **annoncer**) suit.
