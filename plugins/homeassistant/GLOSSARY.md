# Plugin Home Assistant — glossaire

Vocabulaire de domotique qui n'appartient qu'au plugin `homeassistant`. Le terme **Intent**
reste au glossaire racine : il décrit la commande sémantique que le noyau voit passer, pas
une particularité de Home Assistant. Le vocabulaire du contrat (Plugin, Tool de plugin,
Instruction de plugin) reste lui aussi au centre.

**Tool Assist**:
Un Tool exposé par l'API Assist de Home Assistant. La plupart sont des Intents ; d'autres
lisent l'état de la maison (`GetLiveContext`, `HassGetState`) ou lancent un script. Le
plugin `homeassistant` les expose tels quels : Athena ne filtre pas les Tools Assist aux
seuls Intents et ne les renomme pas.
_Avoid_: Tool HA, outil domotique, function

**Exposition Assist**:
Le périmètre des entités pilotables par Athena, défini dans Home Assistant par son
exposition à Assist. Une entité non exposée est invisible pour Gemini.
_Avoid_: Allow-list, périmètre du plugin, permissions

**Serveur MCP HA**:
Le serveur MCP natif de Home Assistant. Le plugin `homeassistant` en est client : c'est
Athena qui se connecte, jamais Gemini.
_Avoid_: `ha-mcp` (serveur tiers), MCP connecté côté Gemini, API REST HA
