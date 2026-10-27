# Recherche web : un plugin à function tools, pas le grounding Gemini

## Status

accepted

Précise l'ADR-0014 : `websearch` est le deuxième plugin serveur du dépôt et le premier à
n'appeler aucune plateforme maison. Ne modifie aucun autre ADR.

## Contexte

Le glossaire annonce depuis l'origine qu'Athena « cherche sur le web », et rien ne
l'implémente : aucun Tool, aucun client HTTP de moteur, aucune clé.

Deux voies existaient. La première est le **grounding** natif de l'API Live
(`google_search`) : pas de clé, pas de crédit, citations fournies par le modèle — mais ce
n'est pas un plugin, il faudrait que l'adaptateur Gemini déclare un outil intégré en plus
des `FunctionDeclaration` qu'il construit aujourd'hui, traduire les metadata de grounding
vers le noyau, et rien ne prouve que Live accepte un builtin et le function calling sur la
même Connexion.

La seconde est un **plugin serveur** au sens de l'ADR-0014 : Gemini appelle un Tool
`web_search`, le serveur interroge un moteur et renvoie le résultat comme
`FunctionResponse`. Le noyau ne change pas d'une ligne, et changer de moteur ne touche
qu'un dossier de plugin.

## Décision

La recherche web est un **plugin serveur** (`plugins/websearch/`), qui expose **un seul**
Tool `web_search` adossé à l'API **Tavily**. Il renvoie au modèle des **Extraits de
recherche** — titre, URL, extrait court, date de publication quand elle existe — et jamais
une page entière : Gemini reste le seul rédacteur de la réponse vocale.

Le plugin est configurable par son manifeste plutôt que par son code : `base_url` (pour
pointer un faux moteur en test ou un proxy), `search_depth`, langue et pays du biais de
classement. Il se sonde au démarrage avec `GET /usage`, qui valide la clé **sans consommer
de crédit**, et lève `PluginUnavailable` quand la clé est refusée ou le réseau muet.

Il met en cache les recherches identiques (TTL court, clé = requête normalisée + paramètres)
et trace dans la Console la requête, ses paramètres, la durée, le nombre de résultats, les
crédits et un `hit`/`miss` de cache — jamais la clé.

Rien ne change côté mémoire : le Journal ne connaît pas le trafic d'outil, donc un résultat
de recherche ne devient durable que par ce qu'Athena en dit.

## Pourquoi

- **Le contrat existait déjà.** Un plugin à function tools est exactement ce que l'ADR-0014
  a construit et ce que `homeassistant` démontre ; le grounding aurait demandé d'inventer
  une seconde catégorie d'outils dans l'adaptateur.
- **Un moteur qui donne du texte, pas des liens.** Tavily renvoie un contenu déjà nettoyé
  par résultat : une seule requête HTTP suffit à nourrir une réponse, là où un moteur
  classique aurait imposé un second appel par page et doublé la latence perçue.
- **Les extraits, pas les pages.** Pour une réponse de trente secondes, dix extraits courts
  couvrent le sujet ; charger des pages entières coûterait des jetons et du temps pour du
  texte qu'Athena ne prononcera jamais.
- **Le moteur est remplaçable.** C'est la seule pièce tiers de l'ensemble : derrière un
  Tool, elle se remplace sans toucher au noyau ni au prompt système.
- **La panne est visible.** Une plateforme tierce tombe et se trompe de clé plus souvent
  qu'un serveur MCP local ; sonder au démarrage la rend visible dans la Console au lieu de
  la faire surgir en pleine conversation (ADR-0014, démarrage dégradé).

## Alternatives écartées

- **`google_search` intégré à l'API Live.** Le moins de code et la meilleure fraîcheur,
  mais pas un plugin : il rouvre l'adaptateur Gemini, exige de traduire des metadata de
  grounding que le noyau n'a aucun concept pour recevoir, et repose sur un mélange
  builtin/function-calling non vérifié pour Live. Un plugin qui marche aujourd'hui vaut
  mieux qu'un pari sur l'API, et le grounding reste possible plus tard pour l'Announce
  comme un second chemin.
- **Brave Search.** Excellents résultats et tarif honnête, mais des extraits courts sans
  texte de page : il aurait fallu un `open_page` dès la v1 pour répondre autrement que par
  des liens.
- **SearXNG auto-hébergé.** Aucune requête ne sortirait de la maison, ce qui est le seul
  argument décisif en sa faveur ; mais il faut l'instance, et la qualité dépend de moteurs
  tiers de toute façon. `base_url` laisse la porte ouverte sans rien promettre.
- **DuckDuckGo HTML.** Zéro clé, mais du scraping hors conditions d'utilisation, cassant au
  premier changement de page.
- **`open_page` (ou `fetch_page`) dès la v1.** Utile pour « raconte-moi cet article », mais
  il ajoute une dépendance d'extraction HTML et une seconde latence dans la boucle pour un
  besoin que les extraits couvrent la plupart du temps.
- **`include_answer` de Tavily.** Fait rédiger la réponse par un second LLM : un crédit de
  plus, deux modèles dans la chaîne, et le contrôle du ton perdu au moment précis où il
  compte — à l'oral.
- **Un Tool de recherche dans le noyau.** Contredit la définition : le noyau est ce qui est
  toujours actif et sans I/O ; la recherche est une intégration externe, donc un plugin.

## Conséquences

- **Les requêtes sortent de la maison.** Chaque recherche part chez Tavily avec la clé de
  l'instance ; c'est le prix du service, et c'est aussi pourquoi la Console trace la requête
  pour que le propriétaire sache ce qui a été demandé.
- **Une clé, donc un secret.** `ATHENA_PLUGIN_WEBSEARCH_API_KEY` ; absente, le démarrage
  échoue (secret requis), refusée, le plugin démarre dégradé et retente toutes les 5 s.
- **Un crédit par recherche non cachée.** `search_depth: basic` en coûte un, `advanced`
  deux ; le cache et la trace des crédits restants sont la seule défense contre la surprise
  en fin de mois.
- **Pas de recherche pendant une Annonce.** Une Session d'annonce déclare une liste de Tools
  vide (ADR-0017) : Athena ne peut pas chercher pour appuyer une annonce proactive. C'est
  cohérent avec la mesure anti-injection, pas un oubli à corriger.
- **Le glossaire gagne deux termes.** *Recherche web* et *Extrait de recherche* ; la phrase
  d'introduction du glossaire (« cherche sur le web ») devient vraie par ce lot.
- **Le moteur n'est pas une Source.** Le plugin n'émet aucun Événement : surveiller un sujet
  et interpeller l'utilisateur est un Watcher, hors de ce lot.
