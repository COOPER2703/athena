# Google Workspace par refresh tokens statiques

## Status

accepted

Précise l'ADR-0014 : `google-workspace` est un plugin serveur à Tools déclarés par le plugin,
authentifié par des refresh tokens OAuth statiques. Ne modifie aucun autre ADR.

## Contexte

Le plugin `google-workspace` doit lire et écrire les mails et l'agenda de plusieurs **Comptes
Google** depuis un serveur sans navigateur. Trois voies existent : le compte de service avec
délégation à l'échelle du domaine, le flow OAuth exécuté par le plugin au démarrage avec un
token persisté, et l'OAuth 2.0 avec des refresh tokens obtenus une fois hors bande et fournis
comme secrets.

## Décision

Le plugin échange des **refresh tokens statiques** contre des access tokens d'une heure, gardés
en mémoire seulement. Chaque Compte Google a son propre client OAuth. Les credentials sont
trois listes parallèles dans `.env` (`CLIENT_ID`, `CLIENT_SECRET`, `REFRESH_TOKEN`), ordonnées
comme les libellés de la configuration `accounts`. Le plugin ne persiste aucun token et
n'exécute aucun flow d'autorisation.

## Pourquoi

- **Compte de service** : il exige une délégation à l'échelle du domaine et des droits
  d'administrateur Workspace, pour un assistant personnel dont le propriétaire est le seul
  utilisateur. La surface dépasse le besoin et sort du modèle de secrets du plugin.
- **Flow OAuth dans le plugin** : le serveur n'a ni navigateur ni répertoire d'état
  contractuel. Il faudrait persister un token tournant et gérer la réautorisation, alors qu'un
  refresh token classique reste un **secret statique**, comme le jeton Notion (ADR-0019).
- **Listes parallèles** : le chargeur ne découvre pas les secrets dynamiquement et le
  manifeste doit déclarer chaque nom. Trois listes ordonnées permettent un nombre de comptes
  non borné sans fichier de credentials hors du modèle `ATHENA_PLUGIN_*` et sans emplacements
  fixes arbitraires.
- **Un client OAuth par compte** : les Comptes Google peuvent appartenir à des organisations
  Workspace différentes, dont les applications internes sont distinctes.

## Conséquences

- Un refresh token révoqué (mot de passe changé avec des scopes Gmail, accès retiré, inactivité
  de six mois) casse un compte jusqu'à réautorisation. Le plugin trace `AccountUnavailable` et
  rend une phrase française ; les autres comptes restent utilisables.
- Une app OAuth **External + Testing** émet des refresh tokens expirant en **7 jours**. La
  configuration cible est une app **Internal** (Workspace) ou **In production**.
- Ajouter un compte, c'est éditer trois lignes du `.env` et un libellé dans `plugins.json`,
  dans le même ordre. Un écart de longueur entre les listes est fatal au démarrage.
