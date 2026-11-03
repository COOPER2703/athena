# Smoke test — Plugin Notion (pages en voix)

Vérification manuelle de bout en bout du plugin serveur `notion` (spec #111, ADR-0019) :
préparer une connexion Notion, partager une page racine, activer le plugin dans Athena, puis
dérouler le parcours vocal complet — chercher une page, la lire, la modifier, la mettre à la
corbeille et la restaurer. Le plugin parle directement à la **REST Notion** avec un jeton
d'intégration statique : Gemini ne voit jamais Notion, seulement six Tools de page.

Voir `docs/smoke-test-t4-plugins.md` pour le contrat des plugins et `websearch`,
`docs/smoke-test-t5-console.md` pour la Console, `docs/smoke-test-t3-evenements.md` pour les
Événements, `docs/smoke-test-t2-memoire.md` pour la mémoire et
`docs/smoke-test-t1-composition.md` pour la boucle serveur/client d'origine.

## Prérequis

Toutes les commandes se lancent **depuis la racine du dépôt** ; le python de la venv locale
s'écrit donc `.venv/bin/python`.

- Un `GEMINI_API_KEY` valide (modèle Live).
- Le protocole généré : `make generate` (ou `python protocol/proto/generate.py`).
- Un micro et un haut-parleur accessibles au client (`pyaudio`).
- Les dépendances à jour : `uv sync`.
- Un **Compte** et un **Jeton d'accès** Athena de nature `client` (ADR-0017), pour
  `python -m client`. Voir `docs/smoke-test-t1-composition.md` pour l'installation.
- **Un compte Notion** et une **connexion interne** créée pour Athena, avec les capacités
  décrites à l'étape 1.
- Une page Notion racine, partagée avec cette connexion, et son identifiant.
- La Console web est utile pour vérifier l'état du plugin (étape 2.3) ; elle est activée par
  défaut sur `127.0.0.1:8767` et demande un Ticket Console.

### Lire les traces

Les extraits ci-dessous sont rendus **sans l'horodatage**, avec les clés du payload triées
comme le fait le rendu (`render` trie les clés) et les identifiants abrégés par `…`.
`N` désigne un nombre qui dépend de la page ou de l'appel (blocs, caractères, durée) : ce
n'est **pas** une valeur à retrouver à l'identique. Les lignes `DEBUG` n'apparaissent
qu'avec `ATHENA_DEBUG_DECISIONS=1`.

## 1. Préparer Notion

### 1.1 Créer la connexion interne

1. Ouvrez <https://www.notion.so/my-integrations> (ou **Paramètres** → **Connexions** →
   **Développer ou gérer les connexions**).
2. Créez une **nouvelle connexion**, de type **Interne**, rattachée au bon workspace.
   Nommez-la par exemple `Athena`.
3. Dans ses **Capacités**, activez les trois droits dont le plugin a besoin :
   - **Lire le contenu** — lecture des pages, des blocs et des propriétés ;
   - **Mettre à jour le contenu** — ajout, remplacement, renommage, corbeille et
     restauration ;
   - **Insérer le contenu** — création de pages.
4. Enregistrez, puis copiez le **jeton d'intégration interne**. Il ne s'affiche qu'une fois :
   il devient `ATHENA_PLUGIN_NOTION_TOKEN` à l'étape 2. Ne le collez ni dans `plugins.json`,
   ni dans un ticket, ni dans un rapport.

Une connexion sans les droits d'**écriture** peut tout de même démarrer : la première action
qui demande le droit manquant rend une phrase `403` (voir le dépannage). Sans **Lire le
contenu**, en revanche, la sonde de la page racine échoue au démarrage et le serveur s'arrête
en nommant `parent_page_id`. Le partage d'une page, lui, est indépendant des capacités : voir
ci-dessous.

### 1.2 Créer la page racine et la partager

1. Créez une page Notion ordinaire, par exemple **« Athena — bac à sable »**. Elle sert de
   parent par défaut et de racine à tout ce que le parcours va créer.
2. Ouvrez ses `•••` (menu de la page) → **Connexions** → **Ajouter une connexion**, puis
   choisissez `Athena` et confirmez.
3. Vérifiez que la page apparaît bien dans les **Connexions** de la page. C'est ce partage
   qui donne accès au contenu ; les capacités seules ne suffisent pas.

Le partage est **héréditaire vers le bas** : une page créée sous cette racine, ou déplacée
dessous, est visible par la connexion. Une page restée hors de la racine partagée reste
invisible, même si la connexion existe dans le workspace. Chaque racine distincte doit être
partagée séparément.

### 1.3 Relever l'identifiant de la page

Ouvrez la page racine et copiez son URL :

```text
https://www.notion.so/Athena-bac-a-sable-0123456789abcdef0123456789abcdef
```

L'identifiant est le **dernier segment de 32 caractères hexadécimaux** (ici
`0123456789abcdef0123456789abcdef`). Le plugin accepte un identifiant seul (avec ou sans
tirets) ou l'URL entière, et `parent_page_id` accepte l'un comme l'autre. Cet exemple copie
l'identifiant, la forme la plus courte.

## 2. Activer le plugin dans Athena

Le dossier `plugins/notion/` (avec son `plugin.json` et son `plugin.py`) est **déjà dans le
dépôt**. Il ne reste qu'à l'activer, à fournir son jeton et à nommer la page parente.

### 2.1 `plugins.json`

Créez ou complétez `plugins.json` **à la racine du dépôt** (fichier non versionné, à ne pas
commiter) :

```json
{
  "enabled": ["notion"],
  "config": {
    "notion": { "parent_page_id": "0123456789abcdef0123456789abcdef" }
  }
}
```

L'activation est une **liste explicite** : un dossier présent mais non listé n'est jamais
importé. `parent_page_id` est **requis** ; `base_url` est la seule autre clé déclarée par le
manifeste et reste **optionnelle** :

```json
{
  "enabled": ["notion"],
  "config": {
    "notion": {
      "parent_page_id": "0123456789abcdef0123456789abcdef",
      "base_url": "http://127.0.0.1:9000"
    }
  }
}
```

`base_url` ne sert qu'aux **tests**, pour pointer un faux serveur Notion. En usage réel,
omettez-la : le défaut est `https://api.notion.com`. N'inventez pas d'autre clé : le
chargeur n'en lit aucune au-delà du manifeste.

### 2.2 `.env`

Créez ou complétez `.env` à la racine (lu par le serveur, ignoré par git) :

```dotenv
GEMINI_API_KEY=votre-cle
ATHENA_USER_ID=owner
# Jeton d'intégration interne Notion. Une variable de processus du même nom
# l'écrase ; un jeton absent est fatal, un jeton refusé est dégradé.
ATHENA_PLUGIN_NOTION_TOKEN=votre-jeton-notion
# Verbeux : indispensable pour voir ToolCall, ToolResult et les Traces du plugin.
ATHENA_DEBUG_DECISIONS=1
```

Le nom du secret est dérivé du manifeste :
`ATHENA_PLUGIN_<PLUGIN>_<SECRET>` → `ATHENA_PLUGIN_NOTION_TOKEN`. Le jeton n'apparaît ni
dans une Trace, ni dans `logs/server.log`, ni dans l'instantané de la Console.

`ATHENA_PLUGIN_NOTION_TOKEN` peut aussi être exportée dans le shell. Un jeton **absent** est
une erreur fatale au démarrage ; un jeton **refusé** par Notion est une panne réparable
(étape 6).

### 2.3 Démarrer et vérifier l'état

```sh
.venv/bin/python -m server
```

Attendu (source `plugins` pour la première ligne, `app` pour la seconde) :

```
INFO [plugins] PluginState {"plugin": "notion", "state": "activated", "tools": 6}
...
INFO [app] started {"state": "Idle"}
```

- `PluginState` est la seule ligne de démarrage propre au plugin : il n'émet ni
  `ToolsDiscovered` ni `InstructionLoaded`.
- Les **six Tools** sont déclarés, dans l'ordre alphabétique de l'instantané :
  `create_page`, `read_page`, `restore_page`, `search_pages`, `trash_page`,
  `update_page`.
- La Console (panneau **Plugins**, après connexion — voir le smoke test T5) affiche
  `notion`, l'état traduit **Activé** et ces six Tools. Aucun jeton, aucun contenu de page.
  L'extrait correspondant de `GET /api/status` est :

```json
{
  "name": "notion",
  "state": "activated",
  "tools": [
    "create_page",
    "read_page",
    "restore_page",
    "search_pages",
    "trash_page",
    "update_page"
  ]
}
```

**Sans redémarrage à chaud** : `plugins.json` est lu au démarrage. Les Tools sont ensuite
déclarés à la **prochaine Connexion Live** — une Session déjà ouverte garde la liste figée à
son ouverture. Si le plugin se répare après une panne, rouvrez donc une Session.

Dans un second terminal, démarrez le client audio :

```sh
export ATHENA_ACCESS_TOKEN=ath_…   # Jeton de nature client
.venv/bin/python -m client
```

Le handshake du transport exige ce Jeton `client` (ADR-0017) ; sans lui, le serveur répond
`401` avant toute trame. Appuyez sur **Entrée** pour ouvrir la Session du parcours, puis
vérifiez la ligne `Activation` :

```
INFO [core] Activation {"connection_id": "s1", "context_chars": 0, "has_context": false, "has_plugin_instruction": true, "plugin_instruction_chars": N, "user_id": "owner"}
```

`Activation` donne seulement la **présence** et la **taille** de l'instruction de plugin
(`has_plugin_instruction`, `plugin_instruction_chars`), jamais son texte. Le plugin fournit
une instruction d'usage qui rappelle les six Tools, les confirmations et le sous-ensemble
Markdown ; le prompt système, l'instruction et le contexte mémoire sont figés à l'ouverture.

## 3. Parcours vocal

Le parcours suppose que la page racine est partagée et que les trois capacités sont
accordées. Les phrases sont des exemples : Athena peut les reformuler, mais les Tools
appelés et leurs effets doivent correspondre.

### 3.1 Chercher une page par titre

Dites : « **Cherche la page Budget** ».

Attendu : Athena rend les titres candidats, avec leur URL, et demande lequel choisir quand
plusieurs se ressemblent. Trace :

```
INFO [plugins] Search {"duration_ms": N, "has_more": false, "plugin": "notion", "query": "Budget", "results": 2, "status": "ok"}
```

- La recherche porte sur les **titres de pages** partagées avec la connexion, triées par
  dernière modification, **dix résultats au plus**. `has_more` et `next_cursor` permettent
  de demander la page suivante ; le modèle peut aussi vous proposer les candidats.
- Un résultat sans titre est rendu « (sans titre) ».
- Si vous donnez déjà une URL ou un identifiant, aucun `search_pages` n'est nécessaire :
  tous les autres Tools acceptent l'un ou l'autre.

### 3.2 Lire une page et « la suite »

Dites : « **Lis la page Budget** ».

Attendu : Athena lit le contenu en Markdown, par blocs de premier niveau (20 par défaut,
bornés à 50), et annonce qu'il reste du contenu si la page est longue. Trace :

```
INFO [plugins] ReadPage {"blocks": 20, "chars": N, "duration_ms": N, "limit": 20, "offset": 0, "page": "…", "plugin": "notion", "status": "ok", "total_blocks": 37, "truncated": false, "unknown_block_ids": []}
```

Dites ensuite : « **La suite** ». Le Tool reçoit `offset` = `next_offset` de l'appel
précédent et poursuit :

```
INFO [plugins] ReadPage {"blocks": 17, "chars": N, "duration_ms": N, "limit": 20, "offset": 20, "page": "…", "plugin": "notion", "status": "ok", "total_blocks": 37, "truncated": false, "unknown_block_ids": []}
```

- `has_more` est faux quand `next_offset` atteint le total. Un tableau et un groupe de
  colonnes ne sont **jamais coupés** par la pagination.
- `truncated` et `unknown_block_ids` sont ceux de Notion, transmis tels quels ; quand ils
  sont posés, le résultat porte une phrase `note` qui prévient que la lecture est
  incomplète. Le plugin ne coupe jamais silencieusement.
- Le contenu de la page n'apparaît **pas** dans la Trace : seuls les comptes et la durée.

### 3.3 Retrouver un mot dans une page

Dites : « **Cherche le mot « assurance » dans la page Budget** ».

Attendu : Athena rend les paragraphes qui contiennent le mot, chacun précédé du **titre de
section le plus proche** au-dessus. Trace :

```
INFO [plugins] ReadPage {"blocks": 2, "chars": N, "duration_ms": N, "limit": 20, "match_count": 2, "offset": 0, "page": "…", "plugin": "notion", "query": "assurance", "status": "ok", "total_blocks": 37, "truncated": false, "unknown_block_ids": []}
```

- La comparaison ignore la casse et les accents, mais exige un **mot entier**.
- Seuls les **paragraphes** sont fouillés : un titre, une liste, un tableau, un bloc de code
  ou une balise Notion n'est jamais une correspondance.
- Avec `query`, `offset` et `next_offset` comptent les **correspondances** ; `match_count`
  donne le total. Une page sans correspondance est un **succès vide** (`match_count: 0`),
  jamais une erreur.
- La recherche ne couvre que **la page demandée** (voir l'étape 4).

### 3.4 Lire les propriétés d'une page de base de données

Donnez l'URL ou l'identifiant d'une page qui appartient à une base, puis dites :
« **Lis les propriétés de cette page** » ou « **Quel est son statut ?** ».

Attendu : `read_page` rend les propriétés en texte, dans l'ordre de la page, en plus du
contenu. Les propriétés vides sont omises. Types rendus : titre, texte enrichi,
sélection/statut, date, nombre, case à cocher (oui/non), URL, e-mail, téléphone, formule et
personnes.

Le plugin **ne touche jamais** aux bases, aux data sources, aux vues ni aux schémas : il lit
la page comme n'importe quelle autre. Seule la propriété de **titre** est écrite (renommage,
étape 3.8) ; les autres propriétés ne sont jamais modifiées.

### 3.5 Créer une page

Sous le parent par défaut, dites : « **Crée une page Courses avec la liste : pain, lait,
café** ».

Attendu : la page naît sous la racine `parent_page_id`, avec un titre et un corps Markdown.
Trace :

```
INFO [plugins] CreatePage {"content_chars": N, "duration_ms": N, "parent": "…", "plugin": "notion", "status": "ok", "title_chars": N}
```

Puis, sous un parent nommé : « **Crée une page Idées sous la page Projets** ». Le modèle
cherche d'abord `Projets` (`search_pages`), vous fait confirmer le parent si nécessaire,
puis passe son identifiant dans `parent`. Trace : `parent` désigne la page `Projets`, pas la
racine configurée.

- Le titre est requis ; le corps est optionnel. Un Markdown refusé, ou plus de 5 000 blocs,
  devient une phrase française et **ne crée rien**.
- Le parent nommé doit être partagé avec la connexion ; sinon, phrase `404` standard.
- La Trace ne contient ni le titre ni le corps, seulement `title_chars` et
  `content_chars`.

### 3.6 Ajouter du contenu sans écraser

Dites : « **Ajoute à la page Courses : ajouter du beurre** » ou « **Complète la page
Budget avec un paragraphe sur le train** ».

Attendu : le contenu existant est conservé et le nouveau Markdown est ajouté **en fin de
page**. Trace :

```
INFO [plugins] UpdatePage {"chars": N, "content_updated": true, "duration_ms": N, "mode": "append", "page": "…", "plugin": "notion", "status": "ok", "title_updated": false, "warnings": 0}
```

- Le mode par défaut est `append` ; l'ajout utilise `insert_content` avec
  `position: { "type": "end" }`.
- Rouvrez la page dans Notion (ou relisez-la) : l'ancien texte est toujours là.
- Les avertissements éventuels de Notion sont transmis dans `warnings` et comptés dans la
  Trace.

### 3.7 Remplacer le corps après confirmation explicite

Dites : « **Remplace tout le contenu de la page Courses par : pain, lait, œufs** ».

Attendu : Athena **demande une confirmation explicite** avant d'écrire. Tant que vous ne
confirmez pas, le Tool refuse et **aucun appel HTTP d'écriture** n'est émis :

```
INFO [plugins] UpdatePage {"chars": 0, "content_updated": false, "duration_ms": N, "mode": "replace", "page": "…", "plugin": "notion", "reason": "replace refused", "status": "error", "title_updated": false, "warnings": 0}
```

Après votre « **oui, remplace** », le modèle rappelle `update_page` avec `mode: "replace"` et
`confirm: true` :

```
INFO [plugins] UpdatePage {"chars": N, "content_updated": true, "duration_ms": N, "mode": "replace", "page": "…", "plugin": "notion", "status": "ok", "title_updated": false, "warnings": 0}
```

- Le remplacement envoie `replace_content` avec `allow_deleting_content: false`. Un contenu
  vide est refusé sans HTTP.
- **Protection des sous-pages** : si le remplacement supprimerait une sous-page ou une base
  enfant, Notion refuse (`400`) et **rien n'est perdu** — le corps d'origine reste intact.
  La phrase le dit explicitement. Un Markdown invalide ou une page trop volumineuse
  produisent le même refus : ne concluez pas à une sous-page sans relire la page.
- Si le titre et le corps sont demandés ensemble et qu'une seule écriture échoue, la
  réponse est `status: "error"` avec une phrase qui nomme ce qui a réussi et ce qui a
  échoué ; `title_updated` et `content_updated` restent lisibles.

### 3.8 Renommer

Dites : « **Renomme la page Courses en Liste de courses** ».

Attendu : `update_page` patche la propriété de titre dans un appel séparé de l'écriture du
corps. Trace :

```
INFO [plugins] UpdatePage {"chars": N, "content_updated": false, "duration_ms": N, "mode": "append", "page": "…", "plugin": "notion", "status": "ok", "title_updated": true, "warnings": 0}
```

- Le renommage est une opération à part : un succès partiel (titre réussi, corps échoué, ou
  l'inverse) est signalé comme tel, jamais passé sous silence.
- Une page sans propriété de titre ne peut pas être renommée : le Tool le dit au lieu de
  deviner.

### 3.9 Mettre à la corbeille après confirmation

Dites : « **Mets la page Liste de courses à la corbeille** ».

Attendu : le Tool lit d'abord la page, compte ses **sous-pages** et **n'écrit rien** tant
que `confirm` n'est pas vrai. La réponse annonce le titre et le nombre :

> La page « Liste de courses » et ses 2 sous-pages seront mises à la corbeille.
> Redemande-moi la suppression avec confirm=true pour confirmer.

(sans sous-page : « La page « … » sera mise à la corbeille. »)

Trace du refus :

```
INFO [plugins] TrashPage {"child_pages": 2, "duration_ms": N, "page": "…", "plugin": "notion", "reason": "confirmation", "status": "error"}
```

Après votre confirmation, le modèle rappelle `trash_page` avec `confirm: true` :

```
INFO [plugins] TrashPage {"child_pages": 2, "duration_ms": N, "page": "…", "plugin": "notion", "status": "ok"}
```

- `child_pages` compte les blocs `child_page` **de premier niveau** (la marche suit le
  curseur Notion). Les sous-pages partent avec la page ; c'est réversible.
- La page disparaît de la vue Notion mais reste dans la **corbeille** ; le résultat porte
  `restorable: true`.
- Sans `confirm: true`, **aucune requête d'écriture** n'est émise (seules les lectures de la
  page et de ses enfants ont lieu).

### 3.10 Restaurer

Dites : « **Restaure la page Liste de courses** ».

Attendu : la page sort de la corbeille et redevient visible. Trace :

```
INFO [plugins] RestorePage {"duration_ms": N, "page": "…", "plugin": "notion", "status": "ok"}
```

Le résultat rend l'identifiant, le titre et l'URL de la page restaurée. Une page non partagée
(ou inexistante) rend la phrase `404` standard.

## 4. Périmètre réel et recherche de contenu

- Le plugin voit **tout ce qui est partagé avec la connexion**, et rien d'autre. Le partage
  est héréditaire vers le bas : les enfants d'une page partagée sont visibles sans partage
  individuel. Une page hors de toute racine partagée reste invisible, même si la connexion
  est installée dans le workspace.
- Aucune configuration n'élargit ce périmètre : ni `parent_page_id`, ni `base_url`, ni une
  clé inventée. `parent_page_id` ne choisit que le parent **par défaut** des créations.
- Un `404` signifie « page inexistante **ou** non partagée avec la connexion » : les deux cas
  sont volontairement indistinguables côté Notion, et la phrase le dit.
- Il n'existe **aucune recherche de contenu dans tout le workspace**. `search_pages`
  n'interroge que les **titres** ; le filtre `query` de `read_page` ne couvre qu'**une page à
  la fois**. Athena n'indexe rien localement : Notion reste la seule source de vérité.
- Les bases de données, data sources, vues, commentaires, déplacements, duplications et
  pièces jointes sont hors périmètre. Une page de base reste une page : on peut la lire et
  lire ses propriétés.
- Le plugin ne pousse **aucun Événement** : c'est une intégration à la demande, pas un
  Watcher ni une Source. Pendant une **Annonce proactive**, l'instruction peut être présente
  mais **aucun Tool Notion n'est déclaré** : un contenu externe ne peut donc pas déclencher
  une écriture.

## 5. Risques de plateforme

- **`v2` est encore une *preview*.** Le plugin demande explicitement
  `markdown_version: "v2"` sur les trois endpoints Markdown (lecture, création, mise à
  jour) pour ne pas dépendre du défaut. La syntaxe `v2` est plus stricte : une erreur de
  parsing peut revenir en `400`. La phrase française invite alors à simplifier le contenu ;
  elle ne réessaie pas.
- **`insert_content` est déprécié.** C'est encore le seul chemin d'ajout Markdown ; le
  plugin l'utilise pour `append`. Un repli vers la conversion Markdown → blocs puis
  `append block children` est anticipé, mais n'est pas implémenté. Si Notion retire
  l'endpoint, un ajout échouera (phrase standard, souvent un `400`) jusqu'à ce repli.
- **`v1` sera retiré de toutes les versions d'API.** Le plugin épingle
  `Notion-Version: 2026-03-11` et demande `v2` : il ne suivra pas silencieusement un
  basculement de défaut. La version d'API est une **constante de code**, jamais une clé de
  configuration.
- **Écritures synchrones uniquement** : `allow_async` n'est jamais activé. Un `503`/`504`
  sur une écriture est traité comme **incertain** (voir le dépannage) ; aucun appel n'est
  rejoué automatiquement.
- **Pas de retry, pas de budget élastique** : 10 s par appel. Un `429`, un `529`, un `5xx`
  ou un délai deviennent une phrase ; c'est à vous de redemander.
- **Markdown** : en écriture, le sous-ensemble garanti est titres 1 à 4, paragraphes,
  listes à puces et numérotées, cases à cocher, citations, blocs de code avec un nom de
  langage court, gras/italique/barré/code en ligne, liens, tableaux GFM (pipe tables) et
  balises `<columns>` / `<column ratio="…">`. En lecture, tout ce que Notion rend en `v2`
  est transmis tel quel. Limites assumées : 2 000 caractères par texte enrichi, 100 blocs
  par ajout, 5 000 blocs à la création ; au-delà, Notion refuse plutôt que de couper.

## 6. Dépannage

| Symptôme | Cause probable | Que faire |
| --- | --- | --- |
| Phrase « Notion a refusé le jeton d'intégration » (`401`) | Jeton faux, révoqué ou d'un autre workspace | Vérifier `ATHENA_PLUGIN_NOTION_TOKEN`, régénérer le jeton si besoin, redémarrer. |
| Phrase « La connexion Notion n'a pas la capacité nécessaire » (`403`) | Capacité manquante | Activer **Lire**, **Mettre à jour** et **Insérer le contenu** sur la connexion. |
| Phrase « Page introuvable ou non partagée » (`404`) | Page absente ou hors du périmètre partagé | Partager la page (ou sa racine) avec la connexion ; les enfants d'une racine partagée sont couverts. |
| `429` / `529` : « Notion limite le débit » | Limite de débit | Attendre quelques instants et redemander. **Aucun retry automatique** : la requête en échec n'est pas rejouée. |
| `400` sur un ajout ou une création | Markdown `v2` invalide, page trop grosse, limite atteinte | Simplifier le contenu ; relire la page avant de réessayer. |
| `400` sur un remplacement | Refus de Notion : protection d'une sous-page ou d'une base enfant, Markdown invalide, page trop grosse | Rien n'a été perdu : relire la page. Ne jamais contourner en supprimant la sous-page. |
| `503` / `504` sur une écriture | Écriture **incertaine** : elle peut avoir été enregistrée | **Relire la page** avant de réessayer ; ne pas rejouer à l'aveugle. |
| Délai de 10 s ou réseau muet | Notion injoignable | La phrase invite à réessayer plus tard ; aucun retry automatique. |
| Le serveur démarre sans les Tools Notion | Notion injoignable ou jeton refusé au démarrage | État `failed` dans la Console, retente toutes les 5 s ; rouvrir une Session quand l'état passe `activated`. |
| Le serveur refuse de démarrer et nomme `parent_page_id` | Page parente introuvable ou non partagée, ou capacité **Lire le contenu** absente : défaut de **configuration** | Partager la page avec la connexion et vérifier la capacité **Lire le contenu**, puis redémarrer. Ce n'est pas une panne réessayable (code de sortie 2). |
| Le serveur refuse de démarrer et nomme `ATHENA_PLUGIN_NOTION_TOKEN` | Secret requis absent | Renseigner la variable (environnement ou `.env`), puis redémarrer (code de sortie 2). |
| Les Tools n'apparaissent pas dans une Session déjà ouverte | Déclarations figées à l'ouverture | Ouvrir une **nouvelle** Session (Entrée). |
| `search_pages` ne trouve pas une page | Recherche par titre seulement, ou page non partagée | Donner l'URL ou l'identifiant de la page ; vérifier le partage. |
| Le mot cherché n'est pas trouvé | Mot entier, paragraphes seulement, page unique | Essayer le mot exact ; les titres, listes, tableaux et blocs de code sont ignorés. |

Démarrage **dégradé** (Notion injoignable, jeton refusé) :

```
WARNING [plugins] ProbeFailed {"plugin": "notion", "reason": "timeout"}
INFO    [plugins] PluginState {"plugin": "notion", "reason": "PluginUnavailable: …", "state": "failed"}
INFO    [app] started {"state": "Idle"}
```

La retente de fond réessaie **toutes les 5 s** et trace `retry: true` à chaque tentative ;
quand Notion revient, l'état passe `activated` sans redémarrage :

```
INFO [plugins] PluginState {"plugin": "notion", "retry": true, "state": "activated", "tools": 6}
```

Un `5xx` sur la sonde `GET /v1/users/me` n'empêche pas l'activation : Notion répond, c'est
sa sonde qui est en défaut. La Trace est
`ProbeDegraded {"plugin": "notion", "status": 500}`. Un jeton absent, lui, n'est jamais
dégradé : c'est une erreur de configuration fatale.

## Vérification automatisée

```sh
make generate
.venv/bin/python -m pytest protocol/tests server/tests client/tests -q
```

Aucun test ne contacte le vrai Notion : un **faux serveur en processus** enregistre chaque
requête (méthode, chemin, en-têtes, corps) et programme les réponses.

- `server/tests/test_plugins_notion_search.py` — manifeste exact, secret absent, jeton
  refusé, Notion injoignable, `5xx` de sonde, `parent_page_id` absent/invalide/non partagé,
  recherche, plafond de dix résultats, curseur, erreurs françaises sans retry, jeton absent
  des Traces/de l'instantané, Annonce sans Tool.
- `server/tests/test_plugins_notion_read.py` — pagination par blocs et reprise, tableau et
  colonnes entiers, propriétés, troncature, `404`, URL, `429` unique, délai.
- `server/tests/test_plugins_notion_query.py` — casse et accents, mot entier, titres de
  section, pagination des correspondances, succès vide, page unique.
- `server/tests/test_plugins_notion_create.py` — parent configuré et parent nommé, titre
  explicite, `v2` à côté du titre, refus de Markdown, parent non partagé, délai.
- `server/tests/test_plugins_notion_update.py` — ajout en fin, renommage séparé, ordre des
  écritures, succès partiels, avertissements, `503` incertain, remplacement sans
  confirmation.
- `server/tests/test_plugins_notion_replace.py` — refus sans `confirm` sans aucun appel
  HTTP, `replace_content` sans suppression, protection d'une sous-page, `503`/`504`, `404`.
- `server/tests/test_plugins_notion_trash.py` — schémas exacts, comptage paginé des
  sous-pages, refus sans écriture, `5xx` incertain, restauration et relecture.
- `server/tests/test_plugins_notion_markdown.py` — tableaux GFM et balises de colonnes
  transmis tels quels, `v2` explicite, Markdown invalide en une phrase.
- `server/tests/test_plugins_notion_transverse.py` — les six schémas et la ligne de Console,
  l'instruction dans le prompt, l'Annonce avec instruction mais sans Tool, le jeton absent
  alors que les Traces de Tool apparaissent, un `429` en une seule requête, un `503`/`504`
  d'écriture incertain et tenté une seule fois.
- `server/tests/test_plugins.py`, `server/tests/test_plugins_loader.py` et
  `server/tests/test_app_integration.py` — registre, états, retente, Tools déclarés à la
  Connexion Live suivante, instantané de la Console.

## Résultat du run

**Non exécuté à ce jour.** Ce document a été rédigé dans un environnement sans workspace
Notion réel, sans jeton d'intégration et sans micro : aucun parcours vocal n'a été joué et
aucune trace réelle n'est reproduite ici. Les extraits décrivent le comportement
**implémenté**, couvert par les tests listés ci-dessus ; les `N` et les identifiants restent
à relever sur un workspace réel. Ce document n'est donc **pas** un compte rendu de run.
