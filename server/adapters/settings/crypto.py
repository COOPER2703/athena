"""Chiffrement au repos des Secrets et clé maître locale (ADR-0024).

AES-GCM d'une bibliothèque éprouvée (``cryptography``), jamais de cryptographie
maison. Le blob stocké porte son format : un octet de version, le nonce, puis le
texte chiffré et son tag. Le nom du Secret sert de donnée associée : un blob
déplacé d'une ligne à l'autre ne se déchiffre pas.

La **clé maître** vit dans un fichier séparé du magasin, en ``0600``. Elle est
générée à la première fois qu'un Secret est enregistré (l'Installation), rendue
une seule fois à l'appelant, puis seulement relue depuis le disque. Une clé
absente ou illisible n'est jamais remplacée en silence : le magasin déclare les
Secrets illisibles et une écriture explicite peut en créer une nouvelle.
"""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 256 bits : la recommandation courante pour AES-GCM.
MASTER_KEY_BYTES = 32
# 96 bits est la taille de nonce recommandée pour GCM.
NONCE_BYTES = 12
# Version du format du blob : la changer permettrait une migration sans
# confondre un ancien blob avec un nouveau.
FORMAT_VERSION = 1
_MASTER_KEY_MODE = 0o600


class MasterKeyFile:
    """Fichier de clé maître : lecture tolérante, création explicite en 0600."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    def read(self) -> bytes | None:
        """Clé décodée, ou ``None`` si le fichier manque ou est inutilisable."""
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        if not raw:
            return None
        try:
            key = base64.urlsafe_b64decode(raw.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return None
        return key if len(key) == MASTER_KEY_BYTES else None

    def ensure(self) -> tuple[bytes, str, bool]:
        """Rend ``(clé, forme encodée, créée)``.

        Une clé existante et valide est rendue sans être réécrite ; seule sa
        création est signalée par ``created=True``. Une clé existante mais
        illisible est remplacée : l'appelant a demandé une écriture explicite,
        qui est le seul chemin de réparation.
        """
        existing = self.read()
        if existing is not None:
            return existing, self._path.read_text(encoding="utf-8").strip(), False

        key = secrets.token_bytes(MASTER_KEY_BYTES)
        encoded = base64.urlsafe_b64encode(key).decode("ascii")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        descriptor = os.open(self._path, flags, _MASTER_KEY_MODE)
        try:
            # ``O_CREAT`` applique le mode au nouveau fichier ; un fichier
            # existant garde le sien, d'où le chmod explicite.
            os.fchmod(descriptor, _MASTER_KEY_MODE)
            os.write(descriptor, (encoded + "\n").encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return key, encoded, True


def seal(key: bytes, name: str, value: str) -> bytes:
    """Chiffre une valeur pour un Secret nommé."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(
        nonce, value.encode("utf-8"), _associated_data(name)
    )
    return bytes([FORMAT_VERSION]) + nonce + ciphertext


def unseal(key: bytes, name: str, blob: bytes) -> str | None:
    """Déchiffre un blob, ou ``None`` s'il est illisible ou altéré."""
    if len(blob) <= 1 + NONCE_BYTES or blob[0] != FORMAT_VERSION:
        return None
    nonce = blob[1 : 1 + NONCE_BYTES]
    ciphertext = blob[1 + NONCE_BYTES :]
    try:
        plaintext = AESGCM(key).decrypt(
            nonce, ciphertext, _associated_data(name)
        )
    except (InvalidTag, ValueError):
        return None
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _associated_data(name: str) -> bytes:
    """Lie le chiffré à son nom : un blob déplacé ne se déchiffre pas."""
    return name.encode("utf-8")
