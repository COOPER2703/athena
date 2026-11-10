# Contacts Google via l'API People

## Status

accepted

Précise l'ADR-0021 : le plugin `google-workspace` ajoute `contacts_search`, qui lit le carnet
d'un **Compte Google** par l'API People. Ne modifie aucun autre ADR.

Amendé ensuite : `contacts_search` ajoute une passe tolérante de secours pour une requête mal
entendue ; le reste de la décision est inchangé.

## Contexte

Pour envoyer un mail à la voix, l'utilisateur nomme une personne (« envoie un message à
Aubane ») ; Gemini a besoin d'une adresse e-mail. Le plugin ne connaissait jusqu'ici que Gmail
et Calendar, tous deux sur `www.googleapis.com`. Trois gisements de contacts existent dans
Google : les contacts **enregistrés** du compte, les **autres contacts** que Gmail mémorise
au fil des échanges, et l'annuaire du domaine Workspace. Chacun a son scope et son endpoint,
et l'API People vit sur un hôte distinct.

## Décision

`contacts_search` interroge `people/me/connections` (scope `contacts.readonly`) et
`otherContacts` (scope `contacts.other.readonly`), puis fusionne les deux réponses sur une
**adresse identique normalisée** : la fiche du carnet garde son nom et marque l'entrée
`carnet`, l'autre source marque `autres`. Une personne sans adresse est écartée. L'annuaire
d'entreprise n'est pas lu. Aucun résultat n'est mis en cache ; seuls les access tokens le
sont, comme avant. L'API People est jointe par une clé de configuration séparée,
`people_base_url` (défaut `https://people.googleapis.com`).

La requête est comparée **localement**, sur la liste déjà téléchargée. Une **passe exacte**
(sous-chaîne par mot, accents et casse ignorés) fait foi ; une **passe tolérante** ne joue
que si elle ne rend rien, et le retour porte alors `approximate: true`. La passe tolérante
rapproche chaque mot d'une **distance d'édition bornée** des mots du nom et des fragments de
la partie locale des adresses (jamais du domaine) : une faute jusqu'à sept lettres, deux à
partir de huit, rien en dessous de quatre, et une distance non nulle exige la **même
initiale**. Elle rend au plus cinq personnes, triées par distance puis par nom. Une
correspondance approximative est relue à voix haute et confirmée avant tout envoi.

## Pourquoi

- **Deux gisements** : le carnet seul rate les personnes qu'on n'a jamais enregistrées mais
  qu'on a déjà contactées ; `otherContacts` seul n'a souvent pas de nom lisible. Les réunir
  donne le meilleur taux de résolution vocale.
- **Fusion par adresse uniquement** : le nom est un mauvais identifiant (homonymes, entrées
  `otherContacts` sans nom). Une adresse identique, elle, ne trompe pas. Deux adresses
  différentes pour un même nom restent deux candidats, que le modèle fait trancher.
- **Pas d'annuaire d'entreprise** : il dépend d'un Workspace administré et d'un scope
  supplémentaire, pour un usage personnel majoritaire.
- **Pas de cache** : le carnet change et une Session dure quelques minutes ; un cache
  introduirait un état à invalider sans gain mesurable.
- **Hôte séparé** : réécrire l'URL de `www.googleapis.com` en `people.googleapis.com` serait
  fragile derrière un proxy ; une clé explicite suit le modèle des deux autres.
- **Tolérance locale, pas `people:searchContacts`** : la requête vient de la reconnaissance
  vocale et arrive parfois à une lettre près (« Aubagne » pour « Aubane »). `searchContacts`
  fait du préfixe, pas de la distance d'édition, et ajouterait un appel réseau pour un carnet
  déjà téléchargé ; la comparaison locale est gratuite et vérifiable en test.
- **Passe tolérante en secours seulement** : la jouer toujours ferait remonter des homonymes
  approximatifs dans les cas où la recherche exacte marchait déjà. Un retour n'est donc jamais
  mélangé : il est exact, ou entièrement approximatif et marqué.

## Conséquences

- Ajouter ces Tools exige une **réautorisation** de chaque Compte Google avec les deux
  scopes, et l'activation de l'API People dans le projet Cloud. Un scope manquant ne casse
  pas Gmail ni l'agenda : le démarrage le trace `ContactsScopeMissing` et le premier appel
  rend une phrase qui nomme la réparation.
- Un nom passé à `gmail_send` n'est plus accepté comme destinataire : le plugin refuse ce qui
  ne ressemble pas à une adresse et renvoie vers `contacts_search`.
- Le choix entre plusieurs adresses ou plusieurs personnes reste une **politique
  d'instruction**, pas une garde serveur : le Tool rend des candidats, Gemini fait choisir, et
  la relecture d'envoi inclut l'adresse résolue pour ne pas demander deux confirmations.
- Ni un nom, ni une adresse, ni la requête cherchée n'apparaissent dans une Trace, le
  journal, l'instantané ou une phrase d'erreur ; seuls le compte, le statut et le nombre de
  résultats sont tracés.
- La tolérance est **figée dans le code**, pas réglable par `plugins.json` : un carnet qui
  chercherait bizarrement à cause d'un seuil oublié serait coûteux à diagnostiquer.
- Une correspondance approximative reste une **proposition** : le drapeau `approximate` et
  l'instruction obligent à relire le nom et l'adresse avant d'envoyer.
