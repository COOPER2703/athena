# Réglages et Secrets : magasin, précédence et application au redémarrage

## Status

accepted

Amende l'ADR-0016 : la Console n'est plus seulement un lecteur ; elle écrit les Réglages et
les Secrets dans un magasin injecté à la racine de composition. Amende l'ADR-0014 : le
chargeur de Plugins résout les secrets aussi dans ce magasin, après l'environnement.

## Contexte

La configuration d'Athena n'existe que sous forme de variables d'environnement et de fichier
`.env`, lue au démarrage dans des modèles gelés. Aucune valeur n'est modifiable en cours de
processus, aucun secret n'est stocké par l'application, et `GEMINI_API_KEY` est si obligatoire
que le serveur refuse de démarrer sans elle.

Cette forme convient à un déploiement piloté par un opérateur, pas à une Console qui doit
permettre à un non-initié de changer la voix, le modèle, la mémoire ou la clé Gemini. Il faut
aussi répondre à trois questions que l'environnement ne pose jamais : où vit un secret, qui
gagne entre une valeur saisie et une valeur d'environnement, et quand un changement prend
effet.

## Décision

### Deux artefacts, séparés de la mémoire et de l'authentification

- Un **magasin de réglages** dédié, à côté de la base mémoire et de la base d'auth : valeurs
  JSON, schéma additif, aucune migration destructive.
- Les **Secrets** sont chiffrés au repos dans ce magasin (AES-GCM d'une bibliothèque éprouvée,
  jamais de cryptographie maison). La **clé maître** est générée à l'Installation, écrite dans
  un fichier séparé en `0600`, et n'est affichée qu'une fois avec l'avertissement de sauvegarde.
- Un déploiement systemd peut fournir la clé maître scellée à la machine via `systemd-creds`,
  en pointant le chemin de la clé vers le répertoire de credentials du service.
- Clé maître perdue : le serveur démarre, déclare les Secrets illisibles et demande de les
  re-saisir. Il ne plante pas et ne réinitialise rien en silence.

### Précédence à quatre niveaux

**Environnement du processus > magasin > `.env` > défauts.**

- Chaque champ expose sa **source** dans l'interface : `default`, `store`, `env` (le `.env`
  étant présenté comme une valeur d'environnement).
- Un champ masqué par l'environnement est en **lecture seule** avec son explication. C'est le
  garde-fou contre le pire des mondes : un écran de réglages qui enregistre sans effet.
- L'environnement reste aussi l'échappatoire de secours : un hôte ou un port mal saisi et
  devenu injoignable se corrige sans toucher au magasin.

### Périmètre éditable

- **Éditables** : voix et modèles (Live, juge, Consolidation), réglages de mémoire et de
  Consolidation, Webhook (activation, hôte, port), hôtes et ports des surfaces, seuils
  d'authentification, niveau de log.
- **En lecture seule** : chemins des bases (mémoire, auth, réglages), répertoires des Plugins
  et des journaux. Aucun réglage dont une mauvaise valeur fait démarrer le serveur sur une base
  vide ou rend la Console injoignable sans recours.
- Un changement d'hôte ou de port exige une confirmation explicite de la nouvelle URL.

### Secrets en écriture seule

- Les routes de Secrets n'acceptent que l'écriture et l'effacement ; aucune ne renvoie une
  valeur. La lecture expose seulement le nom, l'état « configuré » et la date de mise à jour.
- Les Secrets n'entrent ni dans l'instantané, ni dans les Traces, ni dans les messages
  d'erreur, ni dans les journaux.
- La clé Gemini est validée par un appel réel et bon marché avant enregistrement ; les autres
  Secrets sont acceptés sans vérification distante, faute de pouvoir la faire.

### Application

- « Changements en attente » est l'écart entre les valeurs **stockées** et les valeurs
  **chargées au démarrage** : aucun brouillon séparé, donc aucune divergence possible.
- L'application passe par un **redémarrage** du serveur. Quand un drapeau de configuration
  l'autorise, le serveur sort proprement et laisse son superviseur le relancer ; sinon la
  Console affiche le bandeau et la commande exacte.
- Le rechargement à chaud par sous-système n'est pas introduit dans ce lot.

## Pourquoi

- **Chiffré au repos protège le cas réaliste** : un export ou une sauvegarde de la base qui
  fuit sans la machine. Cela ne protège pas d'un hôte compromis — toute application qui peut
  utiliser un secret peut le déchiffrer —, et cette limite est documentée plutôt que masquée.
- **Un fichier de clé séparé** rend la sauvegarde honnête : sauvegarder l'un sans l'autre est
  une erreur visible, pas un silence.
- **La précédence de l'environnement** garde le déploiement reproductible et donne une porte de
  sortie quand un réglage saisi casse l'accès.
- **L'écart stocké/chargé** évite un second état à maintenir, à sérialiser et à expliquer.

## Alternatives écartées

- **Fichier en clair `0600`** (modèle Home Assistant, Grafana, Docker secrets) : le plus simple,
  mais une sauvegarde du répertoire livre la clé Gemini et les secrets de Plugin. Le chiffrement
  coûte une dépendance et une clé, pas plus.
- **Le trousseau du système d'exploitation** (Keychain, DPAPI, libsecret) : excellent pour une
  application de bureau, inutilisable pour un serveur sans session graphique.
- **Mot de passe maître au démarrage** : le plus solide contre le vol de disque, mais il impose
  une déverrouillage humain après chaque redémarrage — incompatible avec un assistant toujours
  allumé.
- **Gestionnaire externe** (Vault, SOPS, KMS cloud) : dépendance et exploitation d'un service de
  plus pour un secret ré-émétable chez le fournisseur.
- **Écrire le `.env`** : perd les commentaires, mélange réglages et secrets, et ne peut pas
  porter de métadonnées (source, date, validation).

## Conséquences

- Nouvelle dépendance de chiffrement, et une clé maître à sauvegarder avec le magasin.
- Deux nouveaux artefacts d'exploitation : le magasin et le fichier de clé. `install.sh` doit
  les créer et les citer.
- L'ADR-0016 est amendé : la Console écrit, mais l'instantané reste la seule lecture de l'état
  du noyau.
- L'ADR-0014 est amendé sur un point : le chargeur de Plugins lit le magasin après
  l'environnement. Le contrat public des Plugins ne change pas.
- Toute route qui expose une valeur de réglage doit porter sa source ; toute route qui touche un
  Secret doit être en écriture seule, et les tests doivent le prouver.
