# Déploiement — contrat d'exploitation de l'Image

Ce document décrit **l'interface entre l'Image Athena et l'orchestrateur** (ici : Ansible).
Il ne contient pas la configuration Ansible elle-même : il contient les valeurs, les
invariants et les pièges qu'une configuration Ansible doit respecter. Tout ce qui est écrit
ici est vérifiable contre le code (`server/app/config.py`, `server/adapters/`) et contre le
`Dockerfile`.

Une seule règle gouverne le reste : **le conteneur est jetable, `/data` et `/logs` ne le
sont pas.**

---

## 1. Ce que l'Image contient

Un seul process (`python -m server`) porte les **trois Surfaces** :

| Surface | Port | Rôle | Route publique |
| --- | --- | --- | --- |
| Console | `8767` | SPA, API JSON, `/api/stream`, `/healthz` | `/` (tout le reste) |
| Webhook | `8766` | `POST /events` pour les sources externes | `/events` |
| Transport | `8765` | Protocole client (WebSocket binaire) | `/ws` |

Séparer les Surfaces en plusieurs conteneurs n'est pas une option de déploiement : elles
partagent une boucle asyncio unique et un état en mémoire (ADR-0005). Un conteneur.

L'Image embarque également, figés à la construction :

- le build de la SPA (`/app/web/dist`) ;
- le code protobuf généré (`/app/protocol/generated`) ;
- le modèle d'embedding `all-MiniLM-L6-v2` dans `/opt/models`.

Conséquence : **le démarrage ne fait aucun appel réseau sortant**, hors Gemini et plugins.
`HF_HUB_OFFLINE=1` est posé : si un modèle manque, le démarrage échoue franchement au lieu
d'attendre un téléchargement.

---

## 2. Cycle de vie de l'Image

- **Architecture** : `linux/amd64` uniquement. Pas de build multi-arch.
- **Construction** : par la CI, sur un runner qui tourne sur l'hôte. Le `docker build`
  écrit directement dans le démon Docker de l'hôte ; **aucun registre n'intervient**.
- **Tags** : `athena:<sha-court-du-commit>` et `athena:latest`.
- **Déploiement** : Ansible recrée le conteneur depuis un **tag SHA épinglé**. `latest`
  existe pour le dépannage, pas pour la production : il ne permet pas de répondre à
  « quelle version tourne ? ».
- **Ansible ne construit jamais l'Image.** Il ne fait que créer/recréer le conteneur.

---

## 3. Le conteneur — référence

Forme canonique, à traduire vers `community.docker.docker_container` :

```sh
docker run -d \
  --name athena \
  --restart unless-stopped \
  --stop-timeout 30 \
  --read-only --tmpfs /tmp \
  --network <reseau-traefik> \
  -v /srv/athena/data:/data \
  -v /srv/athena/logs:/logs \
  -e GEMINI_API_KEY=... \
  -e TZ=Europe/Paris \
  --log-opt max-size=10m --log-opt max-file=3 \
  --label traefik.enable=true \
  ... \
  athena:<sha>
```

Points non négociables :

- **Aucun port publié sur l'hôte.** Traefik joint le conteneur par son IP sur le réseau
  partagé. Si un port doit être publié pour du diagnostic, publier sur
  `127.0.0.1:<port>:<port>` et jamais sur `0.0.0.0`.
- **`--read-only` + `--tmpfs /tmp`.** Le processus n'écrit que dans `/data` et `/logs` ;
  c'est validé en CI par un démarrage réel. Ne pas monter `/app` en écriture.
- **`USER` de l'Image conservé** (`athena`, UID/GID `1000:1000`). Ne pas passer `--user`
  ni `--privileged`.
- **`HEALTHCHECK` fourni par l'Image.** Ne pas le redéfinir ; s'en servir pour attendre
  l'état `healthy` après création (le chargement du modèle prend quelques secondes).
- **Rotation des logs Docker.** `stderr` duplique `logs/server.log` : sans rotation, le
  journal de Docker grossit indéfiniment.

Traduction Ansible attendue (clés `community.docker.docker_container`) :

```yaml
name: athena
image: "athena:{{ athena_image_tag }}"
state: started
recreate: true
restart_policy: unless-stopped
stop_timeout: 30
read_only: true
tmpfs: [/tmp]
user: "1000:1000"
networks:
  - name: <reseau-traefik>
log_driver: json-file
log_options:
  max-size: 10m
  max-file: "3"
env: { ... }            # voir §4
volumes:
  - /srv/athena/data:/data
  - /srv/athena/logs:/logs
labels: { ... }          # voir §6
comparisons: { env: strict }
```

`comparisons: {env: strict}` est important : sans lui, `docker_container` peut considérer
un conteneur comme inchangé alors qu'une variable a disparu de la tâche, et une clé retirée
resterait active.

---

## 4. Variables

### Fournies par Ansible

| Variable | Requis | Valeur |
| --- | --- | --- |
| `GEMINI_API_KEY` | **toujours** | Clé de l'API Gemini. Absente ou vide → sortie code `2` au démarrage. |
| `TZ` | recommandé | `Europe/Paris`. Détermine l'heure locale de la consolidation (`ATHENA_CONSOLIDATION_HOURS=0,12`) et l'horodatage des journaux. Défaut image : UTC. |
| `ATHENA_PLUGIN_<PLUGIN>_<SECRET>` | si le plugin est activé | Un secret déclaré par le manifeste du plugin. `<PLUGIN>` est le nom du plugin en majuscules, tout caractère non alphanumérique remplacé par `_` ; `<SECRET>` est la clé déclarée. Le `README.md` du plugin documente ses variables, leur obligation et sa configuration d'activation. |
| `LOG_LEVEL` | non | `INFO` par défaut. |

Les secrets propres à un plugin suivent la règle générique
`ATHENA_PLUGIN_<PLUGIN>_<SECRET>` ; ce document ne les énumère pas, car ils appartiennent
au plugin et sont documentés dans `plugins/<nom>/README.md`, avec sa configuration
d'activation.

Un secret de plugin manquant **n'est pas toléré** : le démarrage échoue en nommant la
variable et le code de sortie est `2`. C'est volontaire — un plugin activé sans secret est
une erreur de configuration, pas un mode dégradé. (Un plugin dont le *service* est
injoignable, lui, démarre dégradé et se reconnecte toutes les 5 s.)

Les secrets vivent dans l'inventaire ou un vault Ansible, **jamais** dans une image, ni
dans un `env_file` versionné du dépôt.

### Portées par l'Image — ne pas les redéfinir

| Variable | Valeur dans l'Image |
| --- | --- |
| `ATHENA_AUTH_DB` | `/data/auth.db` |
| `ATHENA_MEMORY_DB` | `/data/athena_memory.db` |
| `ATHENA_LOG_DIR` | `/logs` |
| `ATHENA_PLUGINS_STATE` | `/data/plugins.json` |
| `ATHENA_CONSOLE_STATIC_DIR` | `/app/web/dist` |
| `ATHENA_CONSOLE_HOST` | `0.0.0.0` |
| `ATHENA_WEBHOOK_HOST` | `0.0.0.0` |
| `WS_HOST` | `0.0.0.0` |
| `HF_HOME`, `HF_HUB_OFFLINE` | `/opt/models`, `1` |

Ces défauts sont dans l'Image pour une raison : un conteneur qui refuse de démarrer parce
qu'il manque une variable d'implémentation est un piège à incident. Les surcharger est
possible mais c'est alors une décision explicite, à documenter dans l'inventaire.

**Interdits :**

- changer `ATHENA_EMBED_MODEL` : le modèle est figé dans l'Image **et** la mémoire est en
  384 dimensions. Un autre modèle casse `athena_memory.db` ou échoue au démarrage
  (`HF_HUB_OFFLINE=1` empêche tout rattrapage réseau) ;
- remettre `ATHENA_CONSOLE_HOST` / `ATHENA_WEBHOOK_HOST` à `127.0.0.1` : Traefik est dans
  un autre conteneur et verrait un port fermé. La Console et le Webhook sont en loopback
  **par défaut dans le code** ; l'Image corrige ce défaut pour le conteneur.

---

## 5. État : volumes, propriété, contenu

| Chemin hôte | Chemin conteneur | Propriétaire | Contenu |
| --- | --- | --- | --- |
| `/srv/athena/data` | `/data` | `1000:1000`, `0750` | `auth.db`, `athena_memory.db`, `plugins.json` |
| `/srv/athena/logs` | `/logs` | `1000:1000`, `0750` | `server.log` + `.1`…`.3` (rotation 5 Mio) |

Invariants :

- **Les répertoires doivent exister avant le démarrage, avec le bon propriétaire.**
  SQLite ne crée pas le répertoire parent de sa base ; l'absence de `/data` est un échec de
  démarrage, pas une création silencieuse. Côté Ansible :
  `file: state=directory owner=1000 group=1000 mode=0750`.
- **Monter les répertoires, pas les fichiers.** `docker_container` qui recopie
  `plugins.json` remplace l'inode : un conteneur en cours garderait l'ancien fichier. Un
  bind mount de fichier unique se comporte de la même façon au remplacement. Le répertoire
  `/data` est l'unité de montage.
- **`plugins.json` vit dans `/data`** et n'est pas fourni par l'Image. S'il est absent,
  **aucun plugin ne se charge** — silencieusement (`default: []`). C'est le piège numéro un
  de ce déploiement : la Console démarre normalement, mais aucun plugin n'est actif. Ansible
  doit écrire ce fichier (contenu non secret, donc versionnable) :

  ```json
  {
    "enabled": ["<nom>"],
    "config": {
      "<nom>": {}
    }
  }
  ```

  Le `README.md` de chaque plugin (`plugins/<nom>/README.md`) documente sa configuration
  d'activation et ses secrets ; ce document ne duplique pas ces valeurs, qui périsseraient
  ici en silence.

  Retirer un nom de `enabled` désactive le plugin sans redéployer le code ; ajouter un nom
  rend son secret obligatoire (voir §4). Le fichier est relu au **démarrage** : un
  changement exige une recréation du conteneur.

### Sauvegarde

Les deux bases sont indépendantes et se sauvegardent ensemble :

```sh
sqlite3 /srv/athena/data/auth.db ".backup '/backup/auth.db'"
sqlite3 /srv/athena/data/athena_memory.db ".backup '/backup/athena_memory.db'"
```

Une copie de fichier à chaud peut attraper un état incohérent (WAL) ; `.backup` non. Sans
sauvegarde de `auth.db`, un `/data` perdu signifie **un nouveau Code d'installation** et
donc une instance à revendiquer depuis zéro.

---

## 6. Routage Traefik

Un seul hôte, trois routeurs discriminés par le chemin. Hôte : `athena.cooper-host.com`,
TLS terminé par Traefik (certificat existant de l'instance).

```yaml
traefik.enable: "true"
traefik.docker.network: "<reseau-traefik>"

# Console — tout ce qui n'est pas /events ni /ws
traefik.http.routers.athena-console.rule: "Host(`athena.cooper-host.com`)"
traefik.http.routers.athena-console.entrypoints: "websecure"
traefik.http.routers.athena-console.tls: "true"
traefik.http.routers.athena-console.tls.certresolver: "<resolver-existant>"
traefik.http.routers.athena-console.priority: "1"
traefik.http.routers.athena-console.service: "athena-console"
traefik.http.services.athena-console.loadbalancer.server.port: "8767"
traefik.http.services.athena-console.loadbalancer.passhostheader: "true"

# Webhook — POST /events
traefik.http.routers.athena-events.rule: "Host(`athena.cooper-host.com`) && PathPrefix(`/events`)"
traefik.http.routers.athena-events.entrypoints: "websecure"
traefik.http.routers.athena-events.tls: "true"
traefik.http.routers.athena-events.tls.certresolver: "<resolver-existant>"
traefik.http.routers.athena-events.priority: "100"
traefik.http.routers.athena-events.service: "athena-events"
traefik.http.services.athena-events.loadbalancer.server.port: "8766"

# Transport — WebSocket du Client desktop
traefik.http.routers.athena-transport.rule: "Host(`athena.cooper-host.com`) && PathPrefix(`/ws`)"
traefik.http.routers.athena-transport.entrypoints: "websecure"
traefik.http.routers.athena-transport.tls: "true"
traefik.http.routers.athena-transport.tls.certresolver: "<resolver-existant>"
traefik.http.routers.athena-transport.priority: "100"
traefik.http.routers.athena-transport.service: "athena-transport"
traefik.http.services.athena-transport.loadbalancer.server.port: "8765"
```

Invariants :

- **Priorités explicites.** Le routeur Console est un fourre-tout : sans priorité
  supérieure sur `/events` et `/ws`, le résultat dépend de la longueur des règles. `100`
  contre `1` supprime l'ambiguïté.
- **`passhostheader: true`.** La Console compare l'`Origin` de la requête à l'en-tête
  `Host` et répond `403` sur toute mutation si l'hôte est réécrit. Ce n'est pas une
  option de confort : c'est l'authentification qui en dépend (ADR-0017).
- **HTTPS obligatoire.** Le Ticket Console est posé `HttpOnly; Secure; SameSite=Strict`
  sans dérogation possible. Servie en HTTP simple, la Console boucle sur son écran de
  connexion — un cookie `Secure` est silencieusement ignoré par le navigateur.
- **Pas de middleware de compression** sur le routeur Console : il traverse `/api/stream`,
  un WebSocket. Traefik gère l'`Upgrade` seul, sans configuration.
- **Pas de préfixe de chemin.** La SPA référence ses assets en `/assets/...` et appelle
  `/api/...` en absolu. Servir tout ça sous un préfixe demanderait de reconstruire le front.
- **Réseau partagé.** `traefik.docker.network` doit nommer le réseau sur lequel Traefik
  joint le conteneur, sinon le routeur pointe une IP injoignable. Le conteneur ne doit
  appartenir qu'à ce réseau.

---

## 7. Premier démarrage

1. Créer `/srv/athena/{data,logs}` avec `1000:1000`.
2. Si des plugins sont attendus, écrire `/srv/athena/data/plugins.json` (§5).
3. Démarrer le conteneur. Tant qu'aucun Compte n'existe, un **Code d'installation à usage
   unique** est écrit dans le journal :

   ```sh
   docker logs athena 2>&1 | grep '\[auth\] InstallationCode'
   # ou
   grep '\[auth\] InstallationCode' /srv/athena/logs/server.log
   ```

   Il n'est **plus jamais émis** dès qu'un Compte existe. Si `/data` est perdu, il réapparaît.
4. Ouvrir `https://athena.cooper-host.com`, revendiquer l'instance avec ce code, définir le
   mot de passe (12 caractères minimum, Argon2id).
5. Créer les Jetons d'accès depuis la Console :
   - nature **`client`** → pour le Client desktop, qui lit `ATHENA_ACCESS_TOKEN` dans son
     environnement de processus ;
   - nature **`source`** → pour chaque source du Webhook, une par émetteur (le jeton
     identifie la source ; une source contradictoire dans le corps est refusée `403`).

Le Client desktop se configure avec `WS_SERVER_URL=wss://athena.cooper-host.com/ws` — TLS
puisque le jeton voyage en en-tête `Authorization`.

---

## 8. Vérifications après déploiement

| Contrôle | Commande | Attendu |
| --- | --- | --- |
| Santé du conteneur | `docker inspect -f '{{.State.Health.Status}}' athena` | `healthy` |
| Santé publique | `curl -fsS https://athena.cooper-host.com/healthz` | `200` |
| SPA servie | `curl -fsS https://athena.cooper-host.com/ \| head` | l'`index.html` de la SPA, pas la page « build absent » |
| Identité du process | `docker exec athena id` | `uid=1000(athena) gid=1000(athena)` |
| Écritures confinées | `docker exec athena find /app -name '*.db' -o -name '*.log'` | vide |
| Plugins chargés | `grep -i 'plugin' /srv/athena/logs/server.log` | un état par plugin activé |
| Webhook joignable | `curl -i -X POST https://athena.cooper-host.com/events` | `401` (le routeur et l'auth répondent) |
| Transport joignable | handshake `wss://…/ws` sans jeton | `401` + `WWW-Authenticate: Bearer` |

Un `401` sur les deux derniers contrôles est un **succès** : il prouve que le routeur
atteint la Surface et que l'authentification s'applique. Un `502` ou un timeout est un
problème de routage ou de bind.

---

## 9. Pièges connus

| Symptôme | Cause probable |
| --- | --- |
| La Console accepte la connexion mais toute action renvoie `403` | `Host` réécrit : `passhostheader` désactivé, ou un autre proxy devant Traefik. |
| La page de connexion revient en boucle | accès en HTTP : le cookie `Secure` est ignoré. |
| `/events` renvoie une page de SPA | priorité du routeur Console supérieure ou égale à celle du Webhook. |
| Aucun plugin actif, Console normale | `plugins.json` absent de `/data`. |
| Démarrage en code `2` avec un nom de variable | secret de plugin requis par `enabled`. |
| Démarrage en code `2` sans message de secret | `GEMINI_API_KEY` absente ou vide. |
| `unable to open database file` | `/data` absent, ou propriétaire différent de `1000`. |
| Le conteneur démarre mais se vide à chaque recréation | montages manquants (les répertoires créés dans l'Image sont éphémères). |
| La consolidation ne se déclenche pas aux heures attendues | `TZ` non fourni : le conteneur est en UTC. |
| Le conteneur écrit dans `/app` et échoue en lecture seule | une variable de chemin a été surchargée vers un chemin relatif. |

---

## 10. Rollback

L'état est **additif** : le store d'auth crée ses tables en `CREATE TABLE IF NOT EXISTS`,
sans migration (ADR-0017). Revenir à un tag SHA antérieur est donc sûr tant que l'Image
plus récente n'a pas introduit de schéma que l'ancienne ignore — ce que la règle additive
interdit par construction.

Procédure : épingler le SHA précédent dans l'inventaire, recréer le conteneur. `/data` et
`/logs` ne sont pas touchés, donc les Comptes, les Jetons et la mémoire survivent.
