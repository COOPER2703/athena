from __future__ import annotations

from enum import StrEnum


class ClientState(StrEnum):
    """État local du Client.

    - ``LISTENING`` : hors Session, aucun audio n'est transmis.
    - ``ACTIVE`` : Session en cours, l'audio du micro est transmis en continu.
    """

    LISTENING = "listening"
    ACTIVE = "active"
