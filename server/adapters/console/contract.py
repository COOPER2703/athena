"""Contrat JSON et chemins de l'API de la Console web (spec #62, ADR-0002).

Ce module ne dépend de rien : l'adaptateur, le flux WebSocket, l'injection
d'Événements et le paquet ``console`` y lisent les mêmes constantes, sans cycle
d'import entre eux.
"""

from __future__ import annotations

# Enveloppe versionnée du contrat JSON : les clients existants peuvent ignorer
# les champs qu'ils ne connaissent pas, et un changement cassant incrémente
# cette version (ADR-0002, contrat de la console).
ENVELOPE_VERSION = 1
# Préfixe réservé à l'API JSON. Le repli sous ce préfixe renvoie du JSON ; le
# reste du serveur appartient à la SPA.
API_PREFIX = "/api"
STATUS_PATH = "/api/status"
EVENTS_PATH = "/api/events"
STREAM_PATH = "/api/stream"
