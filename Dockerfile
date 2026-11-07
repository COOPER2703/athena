# syntax=docker/dockerfile:1
# check=skip=SecretsUsedInArgOrEnv
#
# Le lint de BuildKit signale toute variable d'environnement dont le nom contient
# « AUTH » comme un secret potentiel. `ATHENA_AUTH_DB` est un chemin de fichier,
# pas un secret : l'exception ci-dessus couvre ce faux positif, et rien d'autre.
#
# Image du Serveur Athena. Un seul process porte les trois Surfaces
# (Transport 8765, Webhook 8766, Console 8767) : c'est l'architecture, pas un
# choix de déploiement. Le contrat avec l'orchestrateur (chemins montés, UID,
# variables à fournir, routage Traefik) est décrit dans docs/deploiement.md.
#
# Aucun secret n'entre dans l'image : ni `.env`, ni clé d'API.

ARG NODE_VERSION=24
ARG PYTHON_VERSION=3.12

# --- 1. Front : la SPA servie par la Console ------------------------------
# Build séparé du reste : le Node n'entre pas dans l'image finale.
FROM node:${NODE_VERSION}-slim AS web
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web/ ./
RUN npm run build

# --- 2. Protocole : messages_pb2.py généré depuis le .proto ---------------
# `protocol/generated/` est gitignoré : il se produit à la construction, comme
# en CI (`make generate`). La version de `grpcio-tools` vient de `uv.lock`, donc
# le code généré correspond exactement à celui que testent les tests.
FROM python:${PYTHON_VERSION}-slim AS proto
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY protocol/proto ./protocol/proto
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
 && uv export --frozen --no-hashes --no-emit-project --extra dev -o /tmp/dev.txt \
 && uv pip install --python /opt/venv/bin/python \
      "$(grep -E '^grpcio-tools==' /tmp/dev.txt)" \
 && /opt/venv/bin/python protocol/proto/generate.py --out /out/protocol/generated

# --- 3. Dépendances Python + modèle d'embedding ---------------------------
# Les paquets viennent du lock (`uv export --frozen`), à une exception près :
# le lock résout toute la pile CUDA de torch sur Linux (`nvidia-*`, `cuda-*`,
# `triton` — près de 1 Go, dont 900 Mo pour `triton` seul). Le serveur est CPU
# seul : on retire ces lignes de la liste et `UV_TORCH_BACKEND=cpu` choisit le
# wheel `+cpu`. Même version, même lock, image ~3x plus légère.
FROM python:${PYTHON_VERSION}-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
# PyAudio est une dépendance déclarée (elle sert au Client desktop, pas au
# Serveur) et n'a pas de wheel Linux : il faut la compiler.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential portaudio19-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
 && uv export --frozen --no-dev --no-hashes --no-emit-project -o /tmp/req.txt \
 && sed -E '/^(nvidia|cuda|triton)[-=_]/d' /tmp/req.txt > /tmp/req-cpu.txt \
 && UV_TORCH_BACKEND=cpu uv pip install \
      --python /opt/venv/bin/python -r /tmp/req-cpu.txt
# Le modèle est figé dans l'image : le démarrage ne dépend d'aucun accès réseau
# à Hugging Face, et un conteneur ne peut pas rester bloqué sur un téléchargement.
ENV HF_HOME=/opt/models
RUN /opt/venv/bin/python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# --- 4. Image finale ------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

# `libportaudio2` accompagne l'extension PyAudio compilée ; `tzdata` rend
# `TZ` effectif (la consolidation se déclenche en heures locales).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libportaudio2 tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# UID/GID fixes : le propriétaire des volumes de l'hôte est connu d'avance,
# donc les scripts de déploiement n'ont pas à interroger l'image.
RUN groupadd --gid 1000 athena \
 && useradd --uid 1000 --gid 1000 --no-create-home \
      --home-dir /app --shell /usr/sbin/nologin athena

WORKDIR /app

COPY --from=deps /opt/venv /opt/venv
COPY --from=deps /opt/models /opt/models
COPY --from=web /build/web/dist /app/web/dist
COPY server/ ./server/
COPY protocol/ ./protocol/
COPY plugin_api/ ./plugin_api/
COPY plugins/ ./plugins/
COPY --from=proto /out/protocol/generated /app/protocol/generated

# Les chemins d'état sont relatifs au répertoire de travail : ils doivent être
# absolus ici, sinon SQLite écrirait dans /app (couche image, perdue au
# recréation du conteneur). Les Surfaces, elles, doivent écouter partout : un
# proxy dans un autre conteneur ne peut pas joindre 127.0.0.1.
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/models \
    HF_HUB_OFFLINE=1 \
    WS_HOST=0.0.0.0 \
    ATHENA_WEBHOOK_HOST=0.0.0.0 \
    ATHENA_CONSOLE_HOST=0.0.0.0 \
    ATHENA_CONSOLE_STATIC_DIR=/app/web/dist \
    ATHENA_AUTH_DB=/data/auth.db \
    ATHENA_MEMORY_DB=/data/athena_memory.db \
    ATHENA_LOG_DIR=/logs \
    ATHENA_PLUGINS_STATE=/data/plugins.json

# Répertoires d'état créés dans l'image pour qu'un `docker run` sans montage
# démarre quand même. Un déploiement réel les remplace par des bind mounts
# (`docs/deploiement.md`) : sinon l'état disparaît avec le conteneur.
RUN mkdir -p /data /logs && chown 1000:1000 /data /logs

EXPOSE 8765 8766 8767

# /healthz est public et vit sur la Console : c'est le seul point de contrôle
# qui ne demande pas de Jeton. `start-period` absorbe le chargement du modèle.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8767/healthz', timeout=3).status == 200 else 1)"]

USER athena

ENTRYPOINT ["python", "-m", "server"]
