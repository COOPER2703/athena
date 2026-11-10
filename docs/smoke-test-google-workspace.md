# Smoke test — Plugin Google Workspace (mails, agenda et contacts en voix)

Vérification manuelle de bout en bout du plugin serveur `google-workspace`
(spec #135, ADR-0021, ADR-0022) : créer un client OAuth par organisation,
obtenir un refresh token hors bande avec le Playground, activer un ou plusieurs
Comptes Google dans Athena, puis dérouler les parcours Gmail, Agenda et
Contacts à la voix.

Le plugin échange des refresh tokens statiques contre des access tokens d'une
heure, gardés **en mémoire seulement**. Il n'exécute aucun flow OAuth, ne
persiste aucun token et ne pousse aucun Événement (ni Source ni Watcher en v1).

Voir `docs/smoke-test-notion.md` pour la forme générale d'un smoke test de
plugin, `docs/smoke-test-t4-plugins.md` pour le contrat des plugins et
`docs/smoke-test-t5-console.md` pour la Console.

## Prérequis

Toutes les commandes se lancent **depuis la racine du dépôt** ; le python de la
venv locale s'écrit donc `.venv/bin/python`.

- Un `GEMINI_API_KEY` valide (modèle Live).
- Le protocole généré : `make generate` (ou
  `python protocol/proto/generate.py`).
- Un micro et un haut-parleur accessibles au client (`pyaudio`).
- Les dépendances à jour : `uv sync`.
- Un **Compte** et un **Jeton d'accès** Athena de nature `client` (ADR-0017),
  pour `python -m client`.
- Un projet Google Cloud et, par organisation, un **client OAuth de type
  « Web application »** (étape 1).
- Les APIs **Gmail**, **Google Calendar** et **People** activées sur ce projet.
- Un **Compte Google** par libellé voulu, avec un refresh token chacun.
- La Console web est utile pour vérifier l'état du plugin et de ses Tools ;
  elle est activée par défaut sur `127.0.0.1:8767` et demande un Ticket Console.

### Lire les traces

Les extraits ci-dessous sont rendus **sans l'horodatage**, avec les clés du
payload triées comme le fait le rendu (`render` trie les clés) et les valeurs
variables abrégées par `…`. `N` désigne un nombre qui dépend du binaire
(Tools déclarés, résultats, durée) : ce n'est **pas** une valeur à retrouver à
l'identique. Les lignes `DEBUG` n'apparaissent qu'avec
`ATHENA_DEBUG_DECISIONS=1`.

## 1. Créer le client OAuth

Chaque organisation Google (Workspace professionnel, Gmail personnel) a son
propre client OAuth. Deux Comptes Google du **même** domaine peuvent partager
un client ; les listes de secrets restent parallèles aux comptes.

1. Ouvrez <https://console.cloud.google.com/> et sélectionnez (ou créez) le
   projet qui portera Athena.
2. **APIs et services** → **Bibliothèque** : activez **Gmail API**,
   **Google Calendar API** et **People API**.
3. **APIs et services** → **Écran de consentement OAuth** :
   - **Type d'utilisateur** : **Interne** pour un domaine Workspace, sinon
     **Externe** ;
   - renseignez le nom de l'application, l'adresse d'assistance et les
     coordonnées du développeur ;
   - ajoutez les cinq scopes de l'étape 2 ;
   - pour un type **Externe**, publiez l'application **En production**
     (voir « Consentement » plus bas).
4. **APIs et services** → **Identifiants** → **Créer des identifiants** →
   **ID client OAuth** :
   - **Type d'application** : **Application Web** ;
   - **URI de redirection autorisés** :
     `https://developers.google.com/oauthplayground` (exactement, sans barre
     oblique finale).
5. Copiez l'**ID client** et le **code secret du client**. Ils deviennent les
   entrées `ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_ID` et
   `ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_SECRET` à l'étape 3. Ne les collez ni
   dans `plugins.json`, ni dans un ticket, ni dans un rapport.

### Consentement : Interne ou En production, jamais Externe + Test

| État de l'application | Refresh token | Verdict |
| --- | --- | --- |
| **Interne** (Workspace) | longue durée | recommandé |
| **Externe** + **En production** | longue durée | accepté (avertissement « application non vérifiée » possible) |
| **Externe** + **Test** | **expire en 7 jours** | **refusé** comme configuration cible |

Une application **Externe + Test** émet des refresh tokens qui meurent au bout
de sept jours : le plugin se dégraderait chaque semaine. Passez l'écran de
consentement **En production** avant de générer les tokens. Les scopes Gmail
sont sensibles : tant que l'application n'est pas vérifiée, Google affiche un
avertissement et plafonne l'usage ; c'est acceptable pour un usage personnel,
pas pour une distribution publique.

## 2. Autoriser chaque Compte dans le Playground

Répétez l'opération pour **chaque** Compte Google, et notez l'ordre des
libellés : les trois listes de secrets suivent cet ordre.

1. Ouvrez <https://developers.google.com/oauthplayground/>.
2. Cliquez sur la **roue dentée** (en haut à droite), cochez **Use your own
   OAuth credentials**, puis collez l'**ID client** et le **code secret** du
   client créé à l'étape 1.
3. Dans **Step 1 — Select & authorize APIs**, saisissez les cinq scopes, un
   par ligne :

   ```text
   https://www.googleapis.com/auth/gmail.readonly
   https://www.googleapis.com/auth/gmail.send
   https://www.googleapis.com/auth/calendar.events
   https://www.googleapis.com/auth/contacts.readonly
   https://www.googleapis.com/auth/contacts.other.readonly
   ```

   - `gmail.readonly` : chercher et lire les messages ;
   - `gmail.send` : envoyer et répondre ;
   - `calendar.events` : lire et écrire les événements d'agenda ;
   - `contacts.readonly` : chercher dans les contacts enregistrés ;
   - `contacts.other.readonly` : chercher dans les autres contacts Gmail.

   Aucun autre scope n'est nécessaire : le plugin ne gère ni brouillons, ni
   étiquettes, ni pièces jointes, ni calendriers secondaires, ni annuaire du
   domaine Workspace.
4. **Authorize APIs**, puis choisissez le Compte Google voulu et acceptez.
5. **Step 2 — Exchange authorization code for tokens** :
   **Exchange authorization code for tokens**.
6. Copiez le **Refresh token** affiché. C'est
   `ATHENA_PLUGIN_GOOGLE_WORKSPACE_REFRESH_TOKEN` pour ce Compte. Il ne
   s'affiche plus ensuite : conservez-le hors du dépôt.
7. Recommencez à l'étape 1 pour le Compte suivant, avec **son** client OAuth si
   l'organisation diffère.

Les valeurs Google ne contiennent pas de virgule : c'est ce qui permet de les
empiler dans une seule variable. Un libellé de compte ne contient pas de
virgule non plus.

## 3. Activer le plugin

### 3.1 `plugins.json`

Le fichier d'état local `plugins.json` (non versionné) liste les plugins
activés et leur configuration **non secrète** :

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

- `accounts` : les libellés, séparés par des virgules, dans l'ordre des listes
  de secrets. Ils sont normalisés en clés (minuscules, accents retirés) : les
  Tools reçoivent `pro` et `perso`.
- `default_account` : facultatif ; absent, le premier compte fait foi. Un
  libellé inconnu est un défaut **fatal**.
- `api_base_url`, `people_base_url` et `token_url` : facultatifs, réservés aux
  tests ou à un proxy ; les défauts sont `https://www.googleapis.com`,
  `https://people.googleapis.com` et `https://oauth2.googleapis.com/token`.

### 3.2 `.env`

Le fichier `.env` (non versionné) porte les trois secrets, chacun une liste
séparée par des virgules, de **même longueur** et dans le **même ordre** que
`accounts` :

```sh
ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_ID=idPro,idPerso
ATHENA_PLUGIN_GOOGLE_WORKSPACE_CLIENT_SECRET=secretPro,secretPerso
ATHENA_PLUGIN_GOOGLE_WORKSPACE_REFRESH_TOKEN=rtPro,rtPerso
```

Un écart de longueur, une entrée vide, un libellé dupliqué après normalisation
ou un `default_account` inconnu est un défaut de **configuration** : le serveur
refuse de démarrer (code de sortie 2) et nomme la variable ou la clé fautive.
Les secrets ne sont jamais affichés.

### 3.3 Démarrer et vérifier

```sh
make generate
.venv/bin/python -m server
```

Attendez la Trace d'activation puis ouvrez la Console :

- `AccountReady` une fois par compte rafraîchi :
  `INFO [plugins] AccountReady {"account": "pro", "plugin": "google-workspace"}` ;
- `PluginState` activé :
  `INFO [plugins] PluginState {"plugin": "google-workspace", "state": "activated", "tools": N}` ;
- la Console montre `google-workspace` **Activé** avec ses Tools (neuf dans la
  cible de la spec : `gmail_search`, `gmail_read`, `gmail_send`, `gmail_reply`,
  `calendar_list_events`, `calendar_create_event`, `calendar_update_event`,
  `calendar_delete_event`, `contacts_search`).

Les Tools ne sont **déclarés qu'à l'ouverture d'une Session** : après un
changement d'état, ouvrez une nouvelle Session (Entrée).

## 4. Parcours vocal

### 4.1 Gmail

1. « Cherche les mails de Camille cette semaine. » → `gmail_search`, puis une
   liste courte (expéditeur, objet, date, extrait).
2. « Lis le dernier. » → `gmail_read` avec l'identifiant rendu par la
   recherche, corps en texte brut ; un message long est annoncé tronqué et les
   pièces jointes ne sont que nommées.
3. « Réponds-lui que je suis d'accord. » → `gmail_reply` : Athena relit les
   destinataires, l'objet et le corps, puis attend un « oui » explicite.
4. « Envoie un mail à … pour dire … » → `gmail_send` après la même relecture.
   Aucun envoi ne part sans confirmation.

### 4.2 Agenda

1. « Qu'est-ce que j'ai demain ? » → si la date du jour n'est pas fournie par le
   contexte, Athena **demande la date** ; sinon `calendar_list_events`.
2. « Crée un rendez-vous jeudi de 14 h à 15 h chez le dentiste. » →
   `calendar_create_event` avec des dates RFC 3339 absolues.
3. « Déplace-le à 16 h. » → `calendar_update_event` en `PATCH` partiel ; seuls
   les champs fournis changent.
4. « Supprime-le. » → `calendar_delete_event` seulement après confirmation
   explicite, avec `confirm=true`.

### 4.3 Contacts

1. « Envoie un message à Aubane pour dire que je serai en retard. » →
   `contacts_search` d'abord ; si une seule adresse correspond, Athena relit
   l'adresse résolue avec l'objet et le corps, puis attend le « oui ».
2. Si Aubane a plusieurs adresses (perso et pro), Athena lit les adresses et
   demande laquelle ; elle n'en choisit jamais une seule de sa propre initiative.
3. Si plusieurs personnes portent le nom, Athena lit les noms et les adresses,
   puis fait choisir.
4. Après le choix, `gmail_send` part avec l'adresse exacte : la confirmation
   d'envoi n'est pas redemandée uniquement pour l'adresse.
5. « Envoie à aubane.crs@gmail.com » → aucune recherche : une adresse explicite
   est utilisée telle quelle.
6. Un nom introuvable → phrase qui le dit, puis proposition de donner l'adresse
   exacte ; Athena n'interroge pas un autre compte sans le demander.

### 4.4 Comptes

- Un compte nommé (« sur le compte perso ») est utilisé ; sinon le compte par
  défaut.
- Si la demande peut viser plusieurs comptes et qu'aucun n'est nommé, Athena
  demande lequel ; elle n'invente jamais un nom de compte.
- Un libellé inconnu rend une phrase française qui liste les comptes
  disponibles, sans aucun appel à Gmail.

## 5. Diagnostiquer un compte en échec

Un refresh token révoqué (`invalid_grant`) n'est **pas** un défaut du plugin :
c'est un Compte Google indisponible. Au démarrage, si au moins un compte
répond, le plugin s'active quand même :

```text
INFO    [plugins] AccountReady {"account": "pro", "plugin": "google-workspace"}
WARNING [plugins] TokenRefreshFailed {"account": "perso", "plugin": "google-workspace", "reason": "invalid_grant"}
WARNING [plugins] AccountUnavailable {"account": "perso", "plugin": "google-workspace", "reason": "invalid_grant"}
INFO    [plugins] PluginState {"plugin": "google-workspace", "state": "activated", "tools": N}
```

Le compte sain continue de répondre. Chaque appel de Tool qui vise le compte
cassé **refait l'échange** et rend une phrase française :

> Le compte « perso » est indisponible : réautorise-le, puis réessaie.

La Trace de l'appel est `ToolCalled` (`status: "error"`) puis `ToolFailed`
(`status: "unavailable"`) ; aucun Gmail n'est contacté pour ce compte. Dès que
le refresh token est réparé, l'appel suivant réussit **sans redémarrer** : la
retente se fait à chaque appel, il n'y a pas de cache négatif.

Si **tous** les comptes échouent, le plugin rend `PluginUnavailable` et le
serveur démarre **dégradé** :

```text
WARNING [plugins] AccountUnavailable {"account": "pro", "plugin": "google-workspace", "reason": "invalid_grant"}
WARNING [plugins] AccountUnavailable {"account": "perso", "plugin": "google-workspace", "reason": "invalid_grant"}
INFO    [plugins] PluginState {"plugin": "google-workspace", "reason": "PluginUnavailable: …", "state": "failed"}
INFO    [app] started {"state": "Idle"}
```

La Console montre `google-workspace` **En échec**, sans Tools. La retente
globale réessaie **toutes les 5 s** et trace `retry: true` à chaque tentative ;
quand un compte revient, l'état passe `activated` sans redémarrage :

```text
INFO [plugins] PluginState {"plugin": "google-workspace", "retry": true, "state": "activated", "tools": N}
```

Un scope Contacts manquant ne dégrade jamais le plugin. Chaque compte sain sonde
son carnet au démarrage et, si Google refuse pour un scope ou une API absente,
trace un avertissement :

```text
WARNING [plugins] ContactsScopeMissing {"account": "perso", "plugin": "google-workspace", "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
```

Le premier appel réel à `contacts_search` rend alors une phrase qui nomme
l'API People et la réautorisation. Gmail et l'agenda continuent de répondre
normalement.

Aucun secret, aucune adresse e-mail, aucun objet, aucun corps, aucune requête
Gmail et aucune URL complète n'apparaissent dans une Trace, le journal
`logs/server.log`, l'instantané de la Console ou une phrase d'erreur. Seuls les
libellés de compte, les codes OAuth connus (`invalid_grant`…), les statuts HTTP,
les types d'exception, les durées et les nombres sont tracés. Un code d'erreur
OAuth inconnu est remplacé par `HTTP 400` : un intermédiaire ne peut pas faire
fuiter de contenu par la raison.

## 6. Réautoriser après révocation ou changement de mot de passe

Un refresh token peut être révoqué par l'utilisateur, par un administrateur,
par un changement de mot de passe (les scopes Gmail sont invalidés) ou par
l'expiration d'une application restée en mode Test.

1. Rejouez l'étape 2 pour le Compte concerné (Playground, mêmes cinq scopes,
   même client OAuth) et copiez le **nouveau** refresh token.
2. Remplacez **seulement** son entrée dans la liste
   `ATHENA_PLUGIN_GOOGLE_WORKSPACE_REFRESH_TOKEN`, à la même position. Ne
   déplacez ni les libellés ni les autres listes.
3. Redémarrez le serveur : les secrets sont résolus au chargement du plugin, et
   la valeur déjà en mémoire ne change pas à chaud. La retente par appel refait
   l'échange avec la valeur **configurée** : tant que `.env` n'est pas relu,
   elle échouera de la même façon.

Vérifiez ensuite `AccountReady` pour ce compte et un appel de Tool réussi. Si
`invalid_grant` persiste, le token appartient à un autre client OAuth (ID ou
secret différent) ou l'application est repassée en mode Test.

## 7. Dépannage

| Symptôme | Cause probable | Que faire |
| --- | --- | --- |
| Le serveur refuse de démarrer et nomme `ATHENA_PLUGIN_GOOGLE_WORKSPACE_*` | Secret requis absent | Renseigner la variable (environnement ou `.env`), puis redémarrer (code de sortie 2). |
| Le serveur refuse de démarrer avec « même longueur » | Listes de secrets désalignées | Compter les entrées : une par compte, même ordre que `accounts`. |
| `default_account` inconnu | Libellé absent ou mal orthographié | Utiliser une clé de la liste `accounts` (ou retirer la clé pour prendre le premier compte). |
| `invalid_grant` sur un compte | Token révoqué, mot de passe changé, application en Test | Réautoriser (étape 6) et remplacer le refresh token. |
| `invalid_client` | ID ou secret faux, ou token d'un autre client | Reprendre l'étape 1 et vérifier les deux listes au même index. C'est un défaut de configuration **du compte** : il reste `AccountUnavailable` ; si tous les comptes échouent, le plugin reste `failed` et retente toutes les 5 s, sans refuser le démarrage. |
| `invalid_scope` ou `access_denied` | Scope manquant ou consentement refusé | Refaire l'étape 2 avec les cinq scopes exacts. |
| Le compte meurt au bout de 7 jours | Application **Externe + Test** | Publier l'écran de consentement **En production**. |
| Phrase 403 « l'accès à ce compte est refusé » | API Gmail ou Calendar désactivée, ou scope absent | Activer les APIs et réautoriser avec les cinq scopes. |
| Phrase « le carnet du compte … n'est pas accessible » | API People désactivée, ou scopes `contacts.*` absents | Activer **People API**, puis réautoriser le compte (étape 6). |
| `ContactsScopeMissing` au démarrage | Même cause, détectée sans casser Gmail ni l'agenda | Réautoriser ; le plugin reste activé entre-temps. |
| Phrase 429 « trop de requêtes » | Quota Google atteint | Attendre ; **aucun retry automatique** sur `403`, `429` ou `5xx`. |
| État `failed`, aucun Tool Google | Aucun compte n'a pu être rafraîchi | Voir les Traces `AccountUnavailable`, corriger, laisser la retente de 5 s agir. |
| La Console montre `Activé` mais un compte échoue | Dégradation partielle attendue | Utiliser un autre compte ; réautoriser celui qui rend `AccountUnavailable`. |
| Les Tools n'apparaissent pas dans une Session déjà ouverte | Déclarations figées à l'ouverture | Ouvrir une **nouvelle** Session (Entrée). |
| Athena invente « demain » | Date du jour absente du contexte | Donner une date absolue ; le noyau n'injecte pas encore la date. |
| Un mail part sans confirmation | L'instruction a été contournée | Vérifier la Session et le prompt ; ne jamais accepter un envoi non relu. |

## Vérification automatisée

```sh
make generate
.venv/bin/python -m pytest server/tests/test_plugins_google_workspace.py -q
```

Aucun test ne contacte le vrai Google : un **faux serveur en processus** imite
le point d'échange OAuth (`POST /token` en formulaire), Gmail, Calendar et
l'API People, enregistre chaque requête et programme les réponses par refresh
token.

Le module `server/tests/test_plugins_google_workspace.py` couvre notamment :

- manifeste exact, secrets requis, défauts de configuration fatals avant tout
  appel réseau ;
- échange OAuth par compte, cache en mémoire, rafraîchissement soixante
  secondes avant expiration, `401` → un rafraîchissement et un rejeu unique ;
- démarrage partiellement dégradé : compte sain utilisable, compte cassé tracé
  `AccountUnavailable`, retenté à **chaque** appel et réparable à chaud ;
- tous les comptes en échec : état `failed`, aucun Tool, retente globale, puis
  activation sans redémarrage ;
- ligne exacte de l'instantané de Console et Tools déclarés à la Session ;
- `contacts_search` : fusion carnet + autres contacts par adresse, nom du
  carnet prioritaire, fiches sans adresse écartées, correspondance par mots
  (accents et casse ignorés), tri, borne et `truncated`, pagination bornée,
  phrase dédiée au scope manquant, sonde `ContactsScopeMissing` non bloquante ;
- `gmail_send` refuse un nom et renvoie vers `contacts_search` ;
- `ToolCalled` puis `ToolFailed` sur chaque échec, aucun `ToolFailed` sur un
  succès ;
- absence de secret, d'adresse, d'objet, de corps, de requête et d'URL dans les
  Traces, l'instantané, les états du registre, les messages d'erreur et le
  journal rendu par `FileTraceSink`.

Les tests de contrat, de chargeur et d'instruction (`test_plugin_contract.py`,
`test_plugins_loader.py`, `test_plugins_instruction.py`) découvrent le plugin
automatiquement.

## Résultat du run

**Non exécuté à ce jour.** Ce document a été rédigé dans un environnement sans
projet Google Cloud réel, sans consentement OAuth et sans micro : aucun parcours
vocal n'a été joué et aucune trace réelle n'est reproduite ici. Les extraits
décrivent le comportement **implémenté**, couvert par les tests listés
ci-dessus ; les `N` restent à relever sur un vrai Compte Google.
