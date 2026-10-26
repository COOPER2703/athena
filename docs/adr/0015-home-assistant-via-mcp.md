# Home Assistant via son serveur MCP

Le plugin `homeassistant` est client du **Serveur MCP HA** natif, sur l'endpoint **Assist**
(Streamable HTTP stateless, `Authorization: Bearer` d'un token longue durée). Athena ne
parle ni REST ni WebSocket à Home Assistant pour piloter : elle découvre les **Tools Assist**
et les appelle.

Les Tools sont exposés **tels quels** — noms, descriptions, schémas — sans filtrage ni
renommage. HA les préfixe déjà (`intent__HassTurnOn`), donc Athena n'ajoute aucun préfixe :
les Tools propres au composant HA sont `homeassistant__*`, les scripts `script__*`, et ces
noms dépendent de la version de Home Assistant. L'**Instruction de plugin** est récupérée
via `prompts/get` : c'est le prompt Assist, qui explique l'usage des Tools (noms de zones,
alias) et qu'aucune description de Tool ne porte.

## Considérations

- Endpoint `/api/mcp/assist` plutôt que `/api/mcp` : pas de compte administrateur requis,
  périmètre exact de l'**Exposition Assist**, et pas de double préfixe (l'endpoint « toutes
  les LLM APIs » fusionne les APIs et préfixe chaque Tool par le nom de l'API).
- Une **session MCP par appel** : le serveur HA est stateless, donc aucune session à
  surveiller et une réparation automatique après un redémarrage de HA.
- Options rejetées : API REST + résolution maison des noms et des zones (duplique HA) ;
  OAuth IndieAuth (le token suffit) ; laisser Gemini se connecter au MCP (le glossaire
  l'interdit) ; proxy stdio local ; allow-list de Tools côté Athena (le périmètre vit dans
  HA) ; ne garder que les Intents (prive des Tools de lecture et des scripts) ; confirmation
  des Tools destructifs en v1.
- Délais : 5 s pour la découverte et l'instruction, 10 s pour un appel. Un échec rend
  `{"status": "error", …}` au modèle, qui peut le dire ; un `isError` de HA remonte son
  texte. Les annotations MCP sont tracées mais inutilisées.
- Limites assumées : HA résout les noms dans **sa** langue configurée (aucune langue n'est
  transmise via MCP) ; le `device_id` du contexte HA n'est pas utilisé ; le MCP ne pousse pas
  d'Événements, donc le plugin n'en émet aucun en v1.
