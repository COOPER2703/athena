# La console web remplace le TUI et devient surface de référence

Il n'y a pas de TUI. Une **console web** — SPA séparée dans `web/`, servie en statique par le
serveur, consommant l'API JSON — assure d'abord le debug et la vue globale (logs, clients,
plugins, mémoire), puis devient la **surface utilisateur** : parler à Athena par message ou
par voix (façon OpenWebUI) et fixer le style des apps Android/iOS.

## Considérations

- Raison : plus simple à maintenir et à faire évoluer que Textual, et l'API qu'elle
  consomme est directement réutilisable par le mobile.
- Conséquence : le serveur n'embarque aucun build JS dans son process ; `web/` vit sa vie.
