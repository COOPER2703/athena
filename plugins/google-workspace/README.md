# Plugin Google Workspace

Le plugin `google-workspace` branche Athena sur **Gmail**, **Google Calendar** et l'**API
People** : Gemini lit, cherche et envoie les mails, consulte et modifie l'agenda, et résout
un nom prononcé en adresse e-mail. Il expose neuf Tools et ne parle jamais à Google depuis
le noyau : chaque Tool part du plugin, avec le client OAuth de son **Compte Google**.

| Domaine | Tools |
| --- | --- |
| Mails | `gmail_search`, `gmail_read`, `gmail_send`, `gmail_reply` |
| Agenda | `calendar_list_events`, `calendar_create_event`, `calendar_update_event`, `calendar_delete_event` |
| Carnet | `contacts_search` |

Les envois de mails et les suppressions d'événements exigent une **confirmation explicite**.
Le plugin n'émet **aucun Événement** en v1 : ni Source ni Watcher, un contenu Google ne peut
donc pas déclencher d'annonce proactive.

Les décisions du plugin sont les ADR locaux [0001](docs/adr/0001-google-workspace-par-refresh-tokens.md)
(refresh tokens statiques) et [0002](docs/adr/0002-contacts-google-via-people-api.md)
(contacts via l'API People). Le vocabulaire — *Compte Google*, *Événement d'agenda*,
*Carnet de contacts*, *Contact Google*, *Correspondance approximative* — est dans le
[glossaire du plugin](GLOSSARY.md).

## Comptes et credentials

Chaque **Compte Google** porte son propre client OAuth ; deux comptes d'un même domaine
Workspace peuvent partager un client. Le plugin échange des **refresh tokens statiques**
contre des access tokens d'une heure, **gardés en mémoire seulement** : il n'exécute aucun
flow OAuth, ne persiste aucun token et ne gère aucune session navigateur.

Les credentials sont **trois listes parallèles** de secrets, ordonnées comme les libellés de
`accounts`. Un écart de longueur, une entrée vide ou un libellé dupliqué après normalisation
est un défaut de configuration **fatal** au démarrage.

## Activation

Le dossier est versionné, mais **rien ne s'exécute tant qu'il n'est pas listé** dans le
fichier d'état local `plugins.json` (non versionné ; `ATHENA_PLUGINS_STATE`,
`/data/plugins.json` dans l'Image). L'activation est lue au démarrage : il n'y a pas
d'activation à chaud.

```json
{
  "enabled": ["google-workspace"],
  "config": {
    "google-workspace": {
      "accounts": "Pro,perso",
      "default_account": "perso"
    }
  }
}
```

- `accounts` est **requis** : les libellés, séparés par des virgules, dans l'ordre des listes
  de secrets. Ils sont normalisés en clés (minuscules, accents retirés) : les Tools reçoivent
  `pro` et `perso`.
- `default_account` est **facultative** ; absente, le premier compte fait foi. Un libellé
  inconnu est fatal.
- `api_base_url`, `people_base_url` et `token_url` sont **réservées aux tests ou à un proxy**.
  Les défauts sont `https://www.googleapis.com`, `https://people.googleapis.com` et
  `https://oauth2.googleapis.com/token`.

## Secrets

Le manifeste déclare trois secrets **requis**, chacun une liste séparée par des virgules, de
**même longueur** et dans le **même ordre** que `accounts` (les valeurs Google ne contiennent
pas de virgule) :

```sh
ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_ID=idPro,idPerso
ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_SECRET=secretPro,secretPerso
ATHENA_PLUGIN_GOOGLE_WORKSPACE_REFRESH_TOKEN=rtPro,rtPerso
```

Ils se fournissent dans l'environnement ou dans `.env` (une variable de processus du même nom
l'écrase). Un secret requis absent fait échouer le démarrage en nommant la variable, avec le
code de sortie `2`. Les trois listes ne sont jamais tracées ni affichées.

## Prérequis côté Google

- un projet Google Cloud avec les APIs **Gmail**, **Google Calendar** et **People**
  activées ;
- un **client OAuth de type « Web application »**, avec l'URI de redirection
  `https://developers.google.com/oauthplayground` (exactement, sans barre oblique finale) ;
- un **refresh token par Compte Google**, obtenu hors bande dans le
  [Playground OAuth](https://developers.google.com/oauthplayground) avec les cinq scopes
  `gmail.readonly`, `gmail.send`, `calendar.events`, `contacts.readonly` et
  `contacts.other.readonly`.

L'API **People** doit être activée dans le projet Cloud : sans elle, Gmail et l'agenda
fonctionnent, mais `contacts_search` rend une phrase qui nomme l'API et la réautorisation.
Un scope Contacts manquant n'empêche **jamais** l'activation (voir plus bas).

**Consentement.** Une application **External + Testing** émet des refresh tokens qui
expirent en **7 jours** : elle est refusée comme configuration cible. La cible est une
application **Interne** (Workspace) ou **External** publiée **En production**. Les scopes
Gmail sont sensibles : une application non vérifiée affiche un avertissement et plafonne
l'usage, ce qui reste acceptable pour un usage personnel.

La recette complète — création du client, consentement, Playground, parcours vocal,
réautorisation et dépannage — est le [smoke test du plugin](docs/smoke-test.md).

## Démarrage, panne partielle et reprise

Au démarrage, `create()` échange **en parallèle** le refresh token de chaque compte. L'état
est tracé par compte puis globalement :

```text
INFO [plugins] AccountReady {"account": "pro", "plugin": "google-workspace"}
INFO [plugins] PluginState {"plugin": "google-workspace", "state": "activated", "tools": 9}
```

**Panne partielle.** Si au moins un compte répond, le plugin s'active. Un compte refusé
(`invalid_grant`, token révoqué, mot de passe changé, application restée en Test) est tracé
`AccountUnavailable`, reste visible dans la Console, et **chaque** appel de Tool qui le vise
refait l'échange avant de rendre une phrase française :

> Le compte « perso » est indisponible : réautorise-le, puis réessaie.

Les autres comptes continuent de répondre, sans cache négatif : dès que le refresh token est
réparé et le serveur redémarré, l'appel suivant réussit sans autre intervention.

**Scope Contacts manquant.** Un refus réparable sur le carnet ne dégrade pas le plugin :
chaque compte sain sonde son carnet au démarrage et trace `ContactsScopeMissing` ; le premier
appel réel à `contacts_search` rend une phrase qui nomme l'API People et la réautorisation.
Gmail et l'agenda restent utilisables.

**Tous les comptes en échec.** Si aucun compte ne peut être rafraîchi, `create()` lève
`PluginUnavailable` : la Console montre le plugin **En échec**, sans Tools, et Athena démarre
quand même. Le serveur retente toutes les **5 s** et s'active seul dès qu'un compte revient,
sans redémarrer Athena.

**Panne en cours de Session.** Un appel qui échoue (Google injoignable, délai dépassé, refus,
réponse illisible) ne lève jamais : il rend au modèle une phrase française qui explique le
refus, et la Session survit. Aucun appel n'est rejoué automatiquement sur `403`, `429` ou
`5xx`.

## Réautoriser un compte

Un refresh token peut être révoqué par l'utilisateur, un administrateur, un changement de mot
de passe ou l'expiration d'une application restée en Test.

1. Rejouez le Playground pour le Compte concerné, avec les **mêmes cinq scopes** et le même
   client OAuth, puis copiez le **nouveau** refresh token.
2. Remplacez **seulement** son entrée dans `ATHENA_PLUGIN_GOOGLE_WORKSPACE_REFRESH_TOKEN`, à
   la même position ; ne déplacez ni les libellés ni les autres listes.
3. Redémarrez le serveur : les secrets sont résolus au chargement, une valeur déjà en mémoire
   ne change pas à chaud. Vérifiez ensuite `AccountReady` pour ce compte et un appel réussi.

Si `invalid_grant` persiste, le token appartient à un autre client OAuth (ID ou secret
différent) ou l'application est repassée en mode Test.

## Contacts

`contacts_search` interroge les contacts **enregistrés** (`people/me/connections`) et les
**autres contacts** que Gmail a mémorisés (`otherContacts`), fusionnés sur une adresse
identique ; il ne voit ni l'annuaire du domaine, ni les fiches sans adresse. La comparaison
est locale : une passe exacte d'abord, puis — seulement si elle ne rend rien — une passe
tolérante qui rapproche chaque mot du nom ou de la partie locale d'une adresse à une faute
près jusqu'à sept lettres, deux à partir de huit, jamais sous quatre lettres et jamais sans
la même initiale (« Aubagne » retrouve « Aubane »). Elle est marquée `approximate: true`,
plafonnée à cinq personnes et triée par distance. Le domaine d'une adresse n'est jamais
comparé, une approximation est relue à voix haute puis confirmée avant tout envoi, et le
carnet n'est pas mis en cache.

Un nom seul n'est jamais une adresse : `gmail_send` refuse un destinataire qui n'y ressemble
pas et renvoie vers le Tool. Quand plusieurs adresses ou plusieurs personnes correspondent,
l'instruction impose de les lire et de faire choisir, jamais de trancher.

## Documentation locale

- [Smoke test du plugin](docs/smoke-test.md) — la recette manuelle de bout en bout.
- [ADR local 0001](docs/adr/0001-google-workspace-par-refresh-tokens.md) — refresh tokens
  statiques plutôt qu'un compte de service ou un flow OAuth.
- [ADR local 0002](docs/adr/0002-contacts-google-via-people-api.md) — contacts via l'API
  People et passe tolérante locale.
- [Glossaire du plugin](GLOSSARY.md) — le vocabulaire Google.
- [Tests](tests/test_plugins_google_workspace.py), [harnais](tests/google_workspace_harness.py)
  et [faux serveur Google](tests/google_stub.py).
