# Instantané de lecture plutôt qu'un port du noyau

## Status

accepted

Précisé par l'ADR-0017 : `GET /api/session` est une route d'**identité** de la Console (état
d'authentification et d'installation), pas un instantané de lecture du noyau. C'est
l'exception explicite à la règle de route de lecture unique ; toute lecture d'état continue de
passer par `App.snapshot()`.

## Contexte

La Console web doit montrer l'état vivant du serveur : Session en cours, Client actif,
Clients branchés, états des Plugins et leurs Tools, configuration non sensible. Elle ne
doit ni parler le Protocole partagé (ADR-0002), ni devenir un Client, ni atteindre le
Coordinator ou `SessionState` (ADR-0003) : les garde-fous AST interdisent déjà ces imports
aux adaptateurs.

Le noyau possède cet état, mais l'exposer par un nouveau **port** — une interface que le
Coordinator implémenterait ou dont il dépendrait — ajouterait une responsabilité de lecture
au noyau pour un seul consommateur, et ferait entrer la forme de la Console dans le domaine.

## Décision

- La racine de composition (`App`) porte une méthode `snapshot()` qui **construit à la
  demande** un dict JSON-ready : `session`, `clients`, `plugins`, `config`.
- L'adaptateur Console reçoit cette méthode comme une **callable** injectée à `start()`,
  exactement comme le Webhook reçoit `push_event`. Il l'appelle à chaque requête et ne
  retient ni n'interprète l'état.
- Le seul ajout au noyau est une propriété de **lecture** : `Coordinator.owner_client_id`,
  qui rend le Client propriétaire de la Session (vide hors Session). Le nom évite
  `active_client_id`, réservé aux garde-fous des adaptateurs ; c'est la racine qui le
  publie sous `session.active_client_id` dans le contrat JSON.
- L'instantané est **gelé** : c'est un dict ordinaire, sans référence aux objets du noyau,
  et sa section `config` ne contient que les clés explicitement choisies par `build_app`.
  Aucun secret n'y entre.
- L'instantané est le point d'extension unique des lectures : mémoire, consolidation ou
  métriques futures s'y ajoutent sans multiplier les routes ni les ports.

## Pourquoi

- Une callable suit le pattern déjà éprouvé de `push_event` : la racine de composition est
  le seul endroit qui connaît à la fois le noyau et les adaptateurs (ADR-0001).
- Le noyau reste pur et importable sans I/O : il ne gagne pas de dépendance vers une forme
  de présentation.
- Un dict sérialisé est trivialement testable par une vraie requête HTTP, sans mock, et
  réutilisable par le futur client mobile (ADR-0002).

## Alternatives écartées

- **Port `Snapshot` dans le noyau** : ajoute une responsabilité de lecture au domaine et
  couple sa forme au consommateur.
- **Accès direct de l'adaptateur au Coordinator** : interdit par le glossaire et les
  garde-fous AST ; ferait de la Console un second noyau.
- **Instantané poussé périodiquement par le noyau** : coût permanent, état potentiellement
  périmé, et aucune raison de réveiller le noyau pour une lecture.
- **Plusieurs routes de lecture** (`/api/session`, `/api/plugins`, …) : le contrat versionné
  veut un instantané unique, étendu sans casser les clients.

## Conséquences

- `GET /api/status` renvoie `{v: 1, session, clients, plugins, config}` ; le `v` est ajouté
  par l'adaptateur, l'instantané ne le porte pas.
- Toute lecture future passe par `App.snapshot()` ; un adaptateur qui voudrait un champ
  supplémentaire le demande à la racine, pas au noyau.
- Le Client propriétaire est lisible sans exposer les transitions de Session : la Console
  observe, elle ne pilote pas.
