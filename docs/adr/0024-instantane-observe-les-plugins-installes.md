# L'instantané observe les plugins installés, chargés ou non

Complète l'ADR-0014 (chargement au démarrage, jamais à chaud) et applique l'ADR-0016
(l'instantané est le point d'extension unique des lectures) au cas des plugins. Ne révise
aucun des deux : la Console reste une surface d'observation, aucun endpoint n'est ajouté.

## Contexte

Le glossaire définit le **Plugin Registry** comme l'ensemble des plugins installés sur le
serveur et de leur état. Le code ne tenait pas cette promesse : le chargeur ne lisait le
manifeste que des plugins retenus par la liste d'activation, et marquait tous les autres
dossiers `disabled` sans jamais savoir ce qu'ils étaient. La Console affichait donc trois
champs — nom, état, noms des Tools — et confondait sous un même mot le dossier simplement
présent, le plugin d'une autre plateforme et le dossier au manifeste cassé.

Un opérateur qui ouvre la Console doit répondre à deux questions : de quoi Athena est-elle
capable en ce moment, et pourquoi tel plugin ne tourne-t-il pas.

## Décision

- Au démarrage, le balayage du répertoire lit le **Plugin Manifest** de **chaque** dossier,
  sans jamais importer son code. Le résultat est une **Déclaration** par dossier, portée
  par le `PluginRegistry` : identité, champs de configuration attendus, noms des secrets
  requis.
- L'**État de service** se lit en six valeurs : `activated`, `failed`, `missing`,
  `inactive`, `unsupported`, `invalid`. `disabled` disparaît. Seuls les trois premiers
  concernent un plugin retenu par la liste d'activation.
- Un manifeste illisible ou incohérent n'est **fatal** que pour un plugin **activé** : il
  l'était déjà (ADR-0014), et le rester évite de perdre une capacité déclarée en silence.
  Pour un dossier non retenu, il donne l'état `invalid` sans interrompre le démarrage.
- L'instantané de lecture expose, par plugin, la Déclaration, l'état, la raison d'échec, la
  retente en cours, l'**Instruction de plugin** et les Tools **par leur nom**. Aucune valeur
  de secret : les secrets sont publiés par leur clé, le nom de leur variable et leur seule
  présence.
- Le diagnostic « prêt à activer » se calcule sans exécuter le plugin : manifeste lisible,
  configuration requise renseignée, secrets requis présents, chaque manque nommé.
- Le balayage n'a lieu qu'au démarrage ; l'instantané sert un état en cache et ne fait
  aucune I/O.

## Pourquoi

- La Console reste une surface d'observation : rien n'est écrit, rien n'est activé à chaud.
- Lire un manifeste n'exécute pas de code : découvrir un plugin inactif ne change rien au
  modèle « déposer un dossier n'exécute rien ».
- Le diagnostic se calcule sans exécuter le plugin, donc il reste vrai pour un plugin qui
  refuse de démarrer.

## Alternatives écartées

- **Balayer le répertoire à chaque lecture** : un dossier déposé à chaud apparaîtrait
  « prêt » alors que le serveur ne le chargera pas avant son redémarrage. Un écran qui ment
  est pire qu'un écran en retard.
- **Rendre tout défaut non fatal** : un plugin activé disparaîtrait silencieusement de la
  capacité d'Athena ; le journal nomme déjà la faute et le code de sortie 2 reste le signal.
- **Une route `GET /api/plugins`** : l'ADR-0016 a écarté les routes de lecture multiples ;
  l'instantané est son point d'extension.
- **Exposer les valeurs de secrets** : contredit l'invariant « un manifeste ne porte jamais
  de valeur de secret » ; la présence suffit à diagnostiquer.

## Conséquences

- Le vocabulaire des états change et le JSON de la Console perd `disabled`. Le seul
  consommateur est la SPA ; le protocole client n'est pas touché.
- L'empreinte de diffusion porte sur l'instantané entier : un changement de Déclaration
  déclenche une trame `status` sans code supplémentaire.
- La lecture au démarrage valide le manifeste de chaque dossier installé, y compris ceux
  qui ne tourneront jamais.
