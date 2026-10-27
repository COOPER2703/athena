"""Lecture d'un en-tête ``Authorization: Bearer``, partagée par les adaptateurs.

Le Webhook et le transport des Clients authentifient un **Jeton d'accès** de la
même façon : un schéma ``Bearer`` (insensible à la casse, RFC 9110) suivi d'un
secret opaque, sans espace. Une seule implémentation évite que les deux
surfaces divergent sur ce qu'elles acceptent.

Ce module est volontairement minimal : ni I/O, ni dépendance au noyau. Il ne
connaît ni le module d'auth ni la nature du Jeton.
"""

from __future__ import annotations

_BEARER_SCHEME = "bearer"


def bearer_secret(header: str | None) -> str | None:
    """Extrait le secret d'un en-tête ``Authorization: Bearer``, ou ``None``.

    Tout en-tête absent, vide, d'un autre schéma, sans séparateur ou dont les
    identifiants contiennent un espace est refusé : un Jeton est un secret
    opaque, jamais une suite de mots. L'appelant traite ``None`` comme un refus
    générique, sans distinguer la cause.
    """
    if not header:
        return None
    scheme, separator, credentials = header.partition(" ")
    if not separator or scheme.lower() != _BEARER_SCHEME:
        return None
    secret = credentials.strip()
    if not secret or any(character.isspace() for character in secret):
        return None
    return secret
