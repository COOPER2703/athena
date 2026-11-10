# Dossier de plugin : ce qu'il possède, et ce qui le rend amovible

## Status

accepted

Complète l'ADR-0014 (contrat `plugin_api`, manifeste, découverte, activation), qui reste
la référence du **chargement**. Cet ADR-ci décrit ce qu'un **dossier** de plugin possède
et à quelle condition il peut être retiré du dépôt sans laisser de trace.

## Contexte

Tout ce qui est propre à un plugin était éparpillé sur trois étages : le code dans
`plugins/<nom>/`, les tests et leurs faux serveurs dans `server/tests/`, la documentation
ailleurs encore (`docs/smoke-test-*.md`, ADR au centre, section d'`AGENTS.md`, entrées de
`GLOSSARY.md`, ligne de `docs/deploiement.md`). Retirer un plugin laissait des tests
orphelins, une recette, un ADR et du vocabulaire ; travailler sur un plugin obligeait à
ouvrir des fichiers du noyau ; et la numérotation centrale des ADR produisait déjà des
collisions entre contextes indépendants.

L'ADR-0014 dit comment un plugin est **chargé**. Il ne dit pas ce qui **appartient** au
plugin. C'est l'objet de cet ADR.

## Décision

Un dossier `plugins/<nom>/` possède :

- son **manifeste** (`plugin.json`) et son **entrypoint** (`plugin.py`), inchangés ;
- son **README**, qui porte l'activation, les secrets et le comportement de démarrage ;
- son **glossaire** local, pour le vocabulaire qui n'appartient qu'à lui ;
- ses **tests**, leur `conftest.py` local et leurs **faux serveurs** externes ;
- son **smoke test**, à côté du code qu'il vérifie ;
- ses **ADR**, numérotés localement à partir de `0001`.

Le centre ne garde que le **générique** : `plugin_api`, le chargeur, l'ADR-0014, cet
ADR-ci, la règle de nommage des secrets, le `plugins/README.md` de convention et l'index
des ADR. L'activation reste dans le fichier d'état central : c'est de l'état de
déploiement, hors du dépôt en production, et il porte de toute façon la configuration non
secrète du plugin. Aucun marqueur d'activation n'entre dans le dossier.

**Critère d'amovibilité.** Un dossier de plugin est amovible quand, hors de lui, il ne
laisse aucune **référence dure** : un import, un chemin construit, une entrée de build ou
de configuration. Une **chaîne inerte** — un nom de plugin utilisé comme valeur de
remplissage dans une fixture du noyau, par exemple — n'est pas une référence dure et
reste tolérée. Le test de contrat des plugins, qui interdit à leur **code** d'importer le
serveur, reste le garde-fou de l'isolation du code.

## Les deux arbitrages assumés

**1. Les tests d'un plugin dépendent de la couture de test du serveur.** Une seule
dépendance de test sort du dossier : le paquet racine `testkit`, qui expose exactement
cinq noms (`RecordingTraceSink`, `FakeJudge`, `wait_until`, `url`, `register`). C'est
intentionnel : les tests de plugin traversent la vraie couture — App, faux Gemini Live,
registre, plugin copié dans un dossier temporaire, faux serveur externe — et ne peuvent
pas la reconstruire sans la dupliquer. `testkit` importe les adaptateurs du serveur (le
transport WebSocket, le contrat d'événements), **pas** `plugin_api` : c'est un nom posé
sur une dépendance qui existait déjà de façon implicite via le `conftest` du serveur. Le
`conftest` des tests du noyau ré-exporte ces cinq noms et garde le reste.

**2. Il n'y a volontairement aucun test de conformité de la forme d'un dossier.** La
présence d'un README, d'un glossaire, d'un smoke test ou d'un dossier de tests n'est
vérifiée par aucun test. La convention est portée par `plugins/README.md` et par le skill
`create-plugin`, qui la produit. Un test de forme ne prouverait pas l'amovibilité — il
faut une revue pour constater l'absence de référence dure — et il couplerait le noyau aux
noms de fichiers de chaque plugin. Le coût assumé : un dossier incomplet se voit en
revue, pas en CI.

## Conséquences

- Les tests, faux serveurs, smoke tests et ADR des plugins déménagent dans leur dossier,
  un plugin par commit, chacun laissant le dépôt vert.
- L'Image exclut les `tests/` et `docs/` des plugins ; le README reste.
- `AGENTS.md` ne décrit plus chaque plugin : il renvoie à `plugins/README.md` et aux
  READMEs des plugins.
- `docs/adr/README.md` tient la règle de numérotation locale et la table de
  correspondance ancien → nouveau.
- La découverte des tests est une seule invocation de pytest pilotée par
  `[tool.pytest.ini_options]`, qui réserve déjà le dossier `plugins`.
