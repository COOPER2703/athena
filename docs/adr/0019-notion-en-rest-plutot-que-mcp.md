# Notion en REST plutôt qu'en MCP

## Status

accepted

Précise l'ADR-0014 : `notion` est un plugin serveur à Tools déclarés par le plugin, pas un
client MCP. Ne modifie aucun autre ADR.

## Contexte

Le plugin `notion` doit lire et modifier le workspace personnel d'un utilisateur depuis un
serveur sans navigateur, sur le plan **Free**. Notion offre trois voies : son serveur **MCP
hébergé** (`https://mcp.notion.com/mcp`), son serveur **MCP open-source** (déprécié), et la
**REST** avec ses endpoints Markdown (`Notion-Version: 2026-03-11`).

## Décision

Le plugin appelle la **REST Notion** directement, avec un **jeton d'intégration interne**
statique. Il épingle `Notion-Version: 2026-03-11` et demande **explicitement**
`markdown_version: "v2"` sur les trois endpoints Markdown. Il expose **six Tools** de page
et ne passe pas par MCP.

## Pourquoi

- **L'authentification.** Sur Free, le jeton MCP est réservé aux plans Business/Enterprise ;
  le MCP hébergé impose donc OAuth : consentement navigateur, refresh **tournant** toutes
  les ~8 h, expiration absolue à 180 jours ou après 30 jours d'inactivité, fichier de jeton
  persistant et réautorisation périodique. La REST tient dans un secret statique.
- **La surface.** Le MCP hébergé expose ~25 Tools — bases de données, vues, commentaires,
  déplacement, duplication, suppression de bases — et le noyau expose les Tools d'un serveur
  MCP tels quels. Cela contredit le périmètre « pages uniquement » et élargit la surface
  destructive d'un assistant vocal.
- **L'ajout non destructif.** `notion-update-page` ne sait que remplacer tout le contenu ou
  faire un chercher/remplacer ; il n'ajoute pas. Le contrat du plugin fait de l'**ajout** le
  mode par défaut. La REST l'offre (`insert_content`), et ses endpoints Markdown rendent déjà
  la lecture/écriture Markdown du MCP.
- **Le serveur open-source** est déprécié par Notion, exige un process Node à côté d'Athena
  et expose du JSON REST brut : aucun gain sur la lecture, et pas de Tool de corbeille dédié.

## Conséquences

- **Pas de recherche de contenu dans tout le workspace.** `POST /v1/search` ne cherche que
  dans les titres. La recherche par mot est limitée à une page à la fois ; un index local
  aurait dupliqué la donnée et contredit « Notion seule source de vérité ».
- **`insert_content` est déprécié** tout en restant le seul chemin d'ajout Markdown. Un
  repli vers la conversion Markdown → blocs puis `append block children` est anticipé.
- **`v1` est déprécié** et sera retiré de toutes les versions d'API à terme ; demander `v2`
  explicitement protège du basculement de défaut, au prix d'une syntaxe encore en *preview*
  et plus stricte (`400` sur erreur de parsing).
- Le contrat des Tools est **indépendant du transport** : si un jeton MCP statique devenait
  disponible ou si Athena passait en Business, la décision pourrait être révisée sans
  toucher au noyau.
