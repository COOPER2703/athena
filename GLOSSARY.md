# Athena — Assistant vocal

Athena est un assistant vocal qui connecte des clients (desktop, Android, iOS, web) à
Gemini Live via un serveur. L'assistant lit et envoie des messages Matrix, contrôle la
domotique, cherche sur le web, mémorise durablement des informations et assiste à
distance le développement logiciel mené par opencode.

Ce glossaire fixe le vocabulaire du domaine. Il porte désormais deux espaces : le
**noyau** (session, mémoire) et les **adaptateurs** qui le relient au monde
(transport, Gemini, Matrix, Home Assistant, console).

## Language

### Noyau

**Noyau**:
L'ensemble des concepts toujours actifs du serveur — cycle de vie des Sessions, mémoire,
jugement des Événements — indépendant de toute I/O. Les intégrations externes sont des
adaptateurs, pas le noyau.
_Avoid_: Core, kernel, moteur, backend

**Session**:
Conversation active et visible par le Client : l'utilisateur et Athena échangent de
l'audio. Commence par un Wake word, un tap ou une Annonce proactive ; se termine par un
signal de fin ou un timeout. La gestion des Sessions est une responsabilité du noyau.
_Avoid_: Conversation, call, chat

**Connexion Live**:
Le lien serveur ↔ Gemini Live, ouvert par le noyau pour tenir une Session ou faire prononcer
une Annonce. Une Connexion Live peut exister sans Session ; une Session n'existe que lorsque
de l'audio est effectivement échangé.
_Avoid_: Session Gemini, session serveur, session interne, session cachée

**Repos**:
État du noyau où aucune Connexion Live n'est ouverte et aucune Session n'est visible. Seul
état où un Événement externe est jugé.
_Avoid_: Idle, inactif, état vide

**Tool**:
Une fonction que Gemini peut appeler pendant une Session. Peut être côté serveur (Matrix,
Home Assistant) ou côté client (shell, URL). Chaque Tool appartient à un plugin ou au
noyau.
_Avoid_: Function, capability, skill

**Tool du noyau**:
Un Tool exécuté par le serveur lui-même, indépendant de tout plugin (ex. mémoire, fin de
Session). Gemini l'appelle comme un autre Tool, mais son exécution ne sort jamais du
serveur et ne touche pas le protocole client.
_Avoid_: tool interne, tool intégré, tool système

**Wake word**:
Le mot déclencheur ("athena") qui active une Session, détecté localement par le Client.
_Avoid_: Trigger word, hotword, activation word

**Veille**:
État d'un Client dont le micro est actif hors Session pour détecter le Wake word, sans
transmettre d'audio. Activée explicitement par l'utilisateur ; distincte de `LISTENING`,
qui décrit l'absence de Session et non l'état du micro.
_Avoid_: Always-on, mode hotword, écoute permanente

**Push-to-talk**:
Mode de démarrage de Session de secours, pour un Client dont la Veille est désactivée :
un tap démarre la Session, un re-tap pendant la Session active déclenche un barge-in.
_Avoid_: Hold-to-talk, bouton d'écoute

**Client state**:
État local d'un Client : `LISTENING` (hors Session, aucun audio transmis) ou `ACTIVE`
(Session en cours, audio transmis en continu). Les transitions sont pilotées par le
client ; la fin de Session vient du serveur.
_Avoid_: AgentState, mode, status

**Playback**:
La lecture audio d'un Vocal Matrix sur le speaker du client. Peut être interrompue par un
Wake word.
_Avoid_: Lecture, diffusion

### Événements

**Événement**:
Un fait externe poussé vers Athena, porteur d'un nom de source et d'un contenu, sans
jugement d'importance. Le LLM juge seul s'il mérite d'interpeller l'utilisateur.
_Avoid_: message, notification, alerte, signal

**Source**:
L'émetteur d'un Événement : un plugin du serveur ou une application externe, identifiée
par un nom fourni par l'émetteur.
_Avoid_: Watcher, intégration, connecteur, app

**Watcher**:
Une Source interne qui surveille activement une ressource par polling, par opposition à
une application externe qui pousse.
_Avoid_: listener, monitor, poller

**Canal d'événements**:
Le transport unique du noyau, par lequel entrent dans l'ordre d'arrivée les entrées des
adaptateurs (audio, cycle de vie, horloge) et les Événements poussés par les Sources. Le
noyau le traite un élément à la fois.
_Avoid_: EventBus, bus, file d'événements

**File d'attente d'Événements**:
Les Événements reçus hors Repos, conservés dans l'ordre d'arrivée en attendant d'être jugés.
Plafonnée et éphémère : les plus anciens sont abandonnés quand elle déborde, et elle ne
survit pas à un redémarrage.
_Avoid_: file d'événements, buffer, queue

**Webhook**:
Le transport HTTP par lequel une application externe pousse un Événement dans le Canal
d'événements. N'est pas un concept du noyau, seulement un moyen d'accès.
_Avoid_: endpoint, callback, hook

**Session automatique**:
Une Session ouverte par le noyau sans Wake word, lorsqu'un Événement est jugé digne d'être
annoncé. Le jugement se fait en texte, hors Session ; la Connexion Live n'est ouverte que
pour faire prononcer l'Annonce, et aucune Session n'apparaît si Athena ne parle pas.
_Avoid_: session proactive, session auto, session serveur, session système

**Annonce proactive**:
La prise de parole non sollicitée d'Athena qui ouvre une Session automatique. Le Client la
reconnaît comme telle et peut se manifester autrement que pour une Session demandée.
_Avoid_: notification, alerte, interpellation

### Matrix

**Room**:
Un salon de discussion Matrix. Identifié de façon canonique par un `room_id` ; le nom
affiché sert à la recherche, jamais de clé.
_Avoid_: Channel, conversation, chat room

**Contact**:
Une personne joignable via Matrix, identifiée par un `user_id`, reliée le cas échéant à
une Room. Un nom de contact n'est pas un identifiant.
_Avoid_: Interlocuteur, correspondant, utilisateur

**Bridge**:
Un pont entre Matrix et un protocole externe (WhatsApp, Instagram, Telegram). Les rooms
bridgées apparaissent comme des rooms Matrix normales.
_Avoid_: Gateway, connector

**Vocal**:
Un message audio dans Matrix.
_Avoid_: Voice message, audio message, message vocal

**Sourdine (Mute)**:
État qui supprime les notifications pour une Room ou globalement. Persisté en mémoire.
_Avoid_: Silencieux, muet

### Domotique

**Intent**:
Une commande sémantique Assist de Home Assistant (`HassTurnOn`, `HassLightSet`, …). La
résolution des noms, zones et alias se fait dans Home Assistant, jamais dans le plugin.
_Avoid_: Command, action, service, appel REST

**Tool Assist**:
Un Tool exposé par l'API Assist de Home Assistant. La plupart sont des Intents ; d'autres
lisent l'état de la maison (`GetLiveContext`, `HassGetState`) ou lancent un script. Le
plugin `homeassistant` les expose tels quels : Athena ne filtre pas les Tools Assist aux
seuls Intents et ne les renomme pas.
_Avoid_: Tool HA, outil domotique, function

**Exposition Assist**:
Le périmètre des entités pilotables par Athena, défini dans Home Assistant par son
exposition à Assist. Une entité non exposée est invisible pour Gemini.
_Avoid_: Allow-list, périmètre du plugin, permissions

**Serveur MCP HA**:
Le serveur MCP natif de Home Assistant. Le plugin `homeassistant` en est client : c'est
Athena qui se connecte, jamais Gemini.
_Avoid_: `ha-mcp` (serveur tiers), MCP connecté côté Gemini, API REST HA

### Identité

**User**:
La personne propriétaire de la mémoire, identifiée par un `user_id` partagé entre tous ses
appareils.
_Avoid_: Client, device, compte

**Client**:
Un appareil connecté au serveur (desktop, Android, iOS, web), identifié par un `client_id`
distinct du `user_id`, nommé par un `client_name` et une `platform`.
_Avoid_: User, device, utilisateur

### Clients

**Client actif**:
Le Client dont la Session est en cours. Seul le Client actif transmet et reçoit l'audio de
la Session.
_Avoid_: Client principal, master

**Client non-actif**:
Un Client connecté au serveur mais sans Session en cours. Peut exécuter ses propres Tools
sur demande du serveur, même quand un autre Client est actif.
_Avoid_: Client inactif, pair

### Mémoire

La mémoire est un composant du noyau, pas un plugin. Elle est toujours disponible et ne
peut pas être désactivée.

**Journal (mémoire court terme)**:
Buffer brut des derniers jours mêlant les échanges des Sessions et les Événements
qu'Athena a annoncés — jamais ceux qu'elle a tus. Recherche sémantique, fenêtre glissante.
Consolidé régulièrement dans la mémoire long terme, à l'exclusion des Événements annoncés
et de la parole qui les annonce.
_Avoid_: Buffer, transcript, mémoire chaude, mémoire temporaire

**Mémoire long terme**:
Faits durables consolidés à partir du Journal, ou issus d'une Mémorisation explicite.
Vecteurs plats, pas de catégories. Recherche sémantique, dédupliquée.
_Avoid_: Warm memory, mémoire tiède, mémoire chaude

**Faits épinglés (Pinned)**:
Sous-ensemble de la mémoire long terme toujours injecté dans le contexte, indépendamment
de la similarité. Contient uniquement le profil de l'utilisateur.
_Avoid_: Hot memory, mémoire chaude, mémoire instantanée

**Rappel (Recall)**:
Recherche sémantique dans la mémoire long terme et le Journal, déclenchée après chaque
énoncé utilisateur ou à la demande.
_Avoid_: Retrieval, lookup

**Activation**:
Application inconditionnelle des Faits épinglés. Distincte du Rappel : ne dépend pas de la
similarité.
_Avoid_: Injection, chargement

**Consolidation**:
Processus nocturne qui lit le Journal, extrait les faits durables non épinglés, déduplique
et les pousse dans la mémoire long terme. N'épingle jamais. Seule écriture automatique de
la mémoire long terme.
_Avoid_: Distillation, extraction nocturne

**Mémorisation explicite**:
Le seul cas où un fait rejoint la mémoire long terme hors Consolidation : l'utilisateur
demande explicitement à Athena de s'en souvenir. Le fait est journalisé et écrit en mémoire
long terme dans la foulée ; s'il relève du profil, il est épinglé.
_Avoid_: save_memory proactif, sauvegarde automatique, mémorisation implicite

### Plugins

Un plugin est un module externe qui expose des Tools à Gemini et peut fournir une
instruction d'usage. Le noyau (session, mémoire) n'est pas un plugin.

**Plugin**:
Un module chargé par le serveur ou un Client, exposant des Tools à Gemini. Chaque plugin a
une seule plateforme (server, desktop, android, ios). Peut être activé, désactivé ou
supprimé. Un plugin activé est du code de confiance : il tourne dans le serveur avec ses
privilèges.
_Avoid_: Extension, addon, module

**Plugin Registry**:
Le catalogue des plugins installés sur le serveur et de leur état (activé, désactivé, en
échec). Il n'existe pas de catalogue de plugins disponibles : installer un plugin, c'est le
rendre présent sur le serveur.
_Avoid_: Plugin list, plugin catalog, marketplace

**Plugin Manifest**:
La description d'un plugin : nom, version, description, plateforme, configuration requise
et noms des secrets requis. Il ne contient jamais de valeur de secret, ne liste pas les
Tools que le plugin expose — ils sont découverts à l'exécution — et ne déclare pas les
Sources qu'il émet : une Source est nommée au moment où l'Événement est poussé.
_Avoid_: Plugin config, plugin metadata

**Instruction de plugin**:
Le texte qu'un plugin fournit pour expliquer à Gemini comment utiliser ses Tools. Le noyau
le compose dans le prompt système de toute Session où les Tools de ce plugin sont déclarés.
Se distingue des descriptions de Tools : c'est une politique d'usage, pas un schéma.
_Avoid_: Prompt de plugin, system prompt du plugin, instructions HA

**Tool de plugin**:
Un Tool exposé par un plugin. Son nom, sa description et son schéma viennent de la
plateforme que le plugin relie ; Athena ne les renomme pas et ne leur ajoute aucun préfixe.
Le noyau l'exécute par le même round-trip qu'un Tool du noyau, mais l'exécution sort du
serveur, vers la plateforme.
_Avoid_: Tool externe, tool intégré, wrapper

### Développement assisté

Athena relaie vocalement le travail qu'opencode mène sur un projet : elle présente les
questions d'opencode, recueille les réponses et déclenche les étapes du pipeline.

**Atelier**:
L'espace de travail isolé d'un projet dans lequel opencode agit, distinct du dépôt de
développement de l'utilisateur.
_Avoid_: Sandbox, workspace, clone, dépôt de travail

**Feature**:
Une unité de travail de développement matérialisée par un dossier contenant le BRIEF, la
spec et les tickets.
_Avoid_: Epic, chantier, projet, tâche

**BRIEF**:
Le document d'intention initial, en langage naturel, qui décrit le changement voulu et sert
d'entrée au grill.
_Avoid_: Cahier des charges, PRD, brief

**Étape**:
Une phase du pipeline de développement (branche, grill, spec, tickets, implémentation, PR),
franchie sur Validation.
_Avoid_: Phase, stage, statut

**Job**:
L'exécution d'opencode sur une issue d'une Feature.
_Avoid_: Run, tâche, ticket

**Campagne**:
L'exécution des Jobs d'une Feature, dans l'ordre de leurs dépendances.
_Avoid_: Batch, run global, implémentation de la feature

**Relais**:
Le rôle d'Athena envers opencode : présenter à l'oral ses questions, recueillir les réponses
de l'utilisateur et les lui retransmettre.
_Avoid_: Proxy, pont, intermédiaire

**Validation**:
L'accord explicite de l'utilisateur qui clôt une Étape et autorise la suivante.
_Avoid_: Approbation, go, confirmation

### Protocole

**Protocole partagé**:
La définition des messages échangés entre le serveur et les clients audio, exprimée en
langage neutre et déclinée par codegen dans chaque langage client. C'est la source de
vérité du fil client audio ↔ serveur.
_Avoid_: Shared library, common code

**Tool client préfixé**:
Un Tool client exposé à Gemini sous la forme `<client_name>__<tool>`, pour que le modèle
adresse un appareil précis. Les Tools serveur ne sont pas préfixés.
_Avoid_: Tool qualifié, tool distant

**Propriétaire du tool (tool owner)**:
Le Client qui a déclaré un Tool client. Seul ce Client peut l'exécuter et renvoyer son
résultat.
_Avoid_: Owner, cible, target

**Motif de fin**:
Le champ `reason` de la fin de Session, qui dit pourquoi une Session se termine. Les valeurs
numériques sont stables : jamais réordonnées ni réutilisées.
_Avoid_: Raison, cause, status

## Relationships

- **Client ↔ Serveur**: Communication WebSocket via le Protocole partagé. Le client envoie
  audio et résultats de Tools, le serveur envoie audio et appels de Tools.
- **Serveur → Gemini**: Connexion Live ouverte pour tenir une Session ou faire prononcer une
  Annonce ; le jugement d'un Événement, lui, se fait via un modèle texte hors Session.
- **Noyau → Session**: Le noyau gère le cycle de vie des Sessions (start, end, timeout).
- **Adaptateurs → Noyau (Canal d'événements)**: Les adaptateurs y déposent leurs entrées
  (audio, cycle de vie, horloge) ; le noyau les traite immédiatement, quel que soit son état.
- **Source → Noyau (Canal d'événements)**: Les plugins et applications externes poussent des
  Événements neutres ; le noyau les met en attente hors Repos et les juge au Repos par un
  modèle texte, puis n'ouvre une Connexion Live que pour faire prononcer l'Annonce : une
  Session automatique n'apparaît qu'au moment de l'Annonce proactive.
- **Événement → Session**: Un Événement n'est jamais injecté dans une Session active ; il
  attend la fin de la Session et est jugé au Repos.
- **Connexion Live → Session**: Une Connexion Live ne devient une Session que si Athena
  parle. Une Connexion Live silencieuse est refermée sans qu'aucun Client passe en `ACTIVE`.
- **Événement → Journal**: Un Événement qu'Athena annonce est écrit dans le Journal ; un
  Événement qu'elle tait ne l'est pas.
