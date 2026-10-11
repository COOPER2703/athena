# Tools du noyau : exécution serveur, round-trip porté par le noyau

Gemini peut appeler des **Tools du noyau** — des Tools qui appartiennent au noyau et non à
un plugin (mémoire : `save_memory`, `forget_memory` ; plus tard la fin de Session). Ils sont
déclarés à l'ouverture de la Connexion Live, exécutés par le serveur, et leur résultat
repart à Gemini par la même connexion. Le **protocole client reste gelé** : un Tool du
noyau ne touche jamais un Client. Le noyau garde la politique — il résout le Tool dans un
registre et décide — tandis que l'adaptateur Gemini ne connaît aucun Tool. Le registre est
un seam générique : les Tools de plugin et de client réutiliseront le même round-trip.

## Considérations

- Options rejetées : laisser l'adaptateur Gemini exécuter les Tools (mélange politique et
  I/O) ; faire transiter les Tools du noyau par le protocole client (gonfle un fil gelé,
  sans besoin).
- Conséquence : nouveaux événements et commandes du noyau (`LlmToolCall`,
  `SendToolResultToLlm`) ; `LlmSocket` reçoit les déclarations de Tools à l'ouverture et sait
  renvoyer un résultat.
