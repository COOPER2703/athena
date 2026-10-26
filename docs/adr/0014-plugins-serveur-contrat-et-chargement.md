# Plugins serveur : manifeste, découverte et activation

Un plugin serveur est un **dossier déposé** dans le répertoire des plugins, décrit par un
**Plugin Manifest** et chargé **au démarrage**, jamais à chaud. Son code tourne dans le
processus du serveur, avec ses privilèges, sans bac à sable : un plugin est du **code de
confiance**. L'activation est une **liste explicite** dans un fichier d'état — déposer un
dossier n'exécute rien tant qu'il n'y est pas listé.

Le plugin reçoit un **PluginContext** (config non secrète, secrets résolus, trace, moyen de
pousser un Événement) et rend un **PluginBinding** (Tools + Instruction de plugin). Il
n'importe aucun interne du serveur et n'écrit pas lui-même dans le registre : le noyau
agrège, détecte les collisions de noms et attribue chaque Tool à son plugin.

Le contrat public vit dans le module **`plugin_api`** (comme `protocol` pour le fil client) :
c'est la seule surface qu'un plugin importe, jamais `server`. Une plateforme injoignable s'y
signale en levant `PluginUnavailable`, seule panne retentable.

## Considérations

- Options rejetées : plugins in-tree (installer = modifier le dépôt) ; paquets Python à
  entry points (packaging et publication prématurés) ; bac à sable ou sous-processus par
  plugin (contredit l'ADR-0005) ; plugins qui s'enregistrent eux-mêmes (aucune attribution,
  aucune détection de collision) ; activation à chaud par API (la liste des Tools est
  déclarée à l'ouverture de chaque Connexion Live — ADR-0008 — donc l'activation à chaud
  ajouterait un état mutable sans besoin).
- Le manifeste ne contient **jamais** de valeur de secret, ne liste pas les Tools (ils sont
  découverts à l'exécution) et ne déclare pas les Sources (une Source est nommée au moment
  où l'Événement est poussé).
- Un défaut de configuration ou de chargement d'un plugin **activé** fait échouer le
  démarrage ; une plateforme injoignable donne un démarrage **dégradé**, tracé, puis une
  retente en tâche de fond.
- Conséquence : nouveaux types purs dans le noyau (`PluginManifest`, `PluginBinding`,
  `ToolBinding`, `PluginRegistry`) et un chargeur dans les adaptateurs. La gestion par
  API/console (activer, désactiver, supprimer) est différée et réutilisera la même
  construction du registre ; « supprimer » restera une opération du système de fichiers.
