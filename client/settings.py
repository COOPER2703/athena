"""Réglages de couplage du Client desktop, persistés hors du dépôt.

Le couple « URL du serveur + Jeton d'accès » est saisi depuis la page locale
(``client.page``) pour éviter d'éditer un fichier. Il survit au redémarrage du
daemon : le fichier est privé (``0600``, dossier ``0700``), écrit de façon
atomique, et le Jeton n'est jamais rendu par l'API de la page.

Précédence, alignée sur celle du serveur : environnement du processus > fichier
local > défaut. ``ATHENA_ACCESS_TOKEN`` reste donc une surcharge prioritaire,
mais n'est plus le seul moyen de coupler un poste.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from client.config import ACCESS_TOKEN_ENV

log = logging.getLogger("athena.client")

# Même variable et même défaut que ``ClientConfig`` : une seule source.
SERVER_URL_ENV = "WS_SERVER_URL"
DEFAULT_SERVER_URL = "ws://localhost:8765"


@dataclass(frozen=True, slots=True)
class StoredCoupling:
    """Contenu du fichier local, sans valeur par défaut implicite."""

    server_url: str = ""
    access_token: str = ""


@dataclass(frozen=True, slots=True)
class Coupling:
    """Couplage effectif et provenance de chaque champ (jamais le Jeton)."""

    server_url: str
    access_token: str
    url_source: str
    token_source: str

    @property
    def token_configured(self) -> bool:
        return bool(self.access_token)


def resolve_coupling(
    stored: StoredCoupling,
    environ: Mapping[str, str] | None = None,
) -> Coupling:
    """Applique la précédence environnement > fichier > défaut."""
    env = os.environ if environ is None else environ
    env_url = env.get(SERVER_URL_ENV, "").strip()
    if env_url:
        server_url, url_source = env_url, "environment"
    elif stored.server_url:
        server_url, url_source = stored.server_url, "file"
    else:
        server_url, url_source = DEFAULT_SERVER_URL, "default"

    env_token = env.get(ACCESS_TOKEN_ENV, "").strip()
    if env_token:
        token, token_source = env_token, "environment"
    elif stored.access_token.strip():
        token, token_source = stored.access_token.strip(), "file"
    else:
        token, token_source = "", "none"

    return Coupling(
        server_url=server_url,
        access_token=token,
        url_source=url_source,
        token_source=token_source,
    )


class SettingsStore:
    """Fichier JSON privé du couplage local.

    Un fichier absent, illisible ou corrompu n'empêche jamais le démarrage :
    la page locale doit pouvoir s'afficher pour le réparer.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path).expanduser()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> StoredCoupling:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return StoredCoupling()
        except OSError as exc:
            log.warning("Réglages de couplage illisibles (%s): %s", self._path, exc)
            return StoredCoupling()
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            log.warning("Réglages de couplage invalides (%s): %s", self._path, exc)
            return StoredCoupling()
        if not isinstance(payload, dict):
            log.warning(
                "Réglages de couplage invalides (%s): objet attendu", self._path
            )
            return StoredCoupling()
        server_url = payload.get("server_url")
        access_token = payload.get("access_token")
        return StoredCoupling(
            server_url=server_url if isinstance(server_url, str) else "",
            access_token=access_token if isinstance(access_token, str) else "",
        )

    def save(
        self,
        *,
        server_url: str,
        access_token: str | None = None,
    ) -> StoredCoupling:
        """Écrit le couplage ; un Jeton vide ou absent conserve le précédent.

        Écriture atomique : un fichier temporaire privé puis ``os.replace``, pour
        qu'une coupure ne laisse jamais un fichier tronqué à la place du bon.
        """
        previous = self.load()
        token = previous.access_token if not access_token else access_token.strip()
        stored = StoredCoupling(server_url=server_url.strip(), access_token=token)

        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            "server_url": stored.server_url,
            "access_token": stored.access_token,
        }
        fd, temporary = tempfile.mkstemp(
            dir=self._path.parent, prefix=".client-", suffix=".json"
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return stored
