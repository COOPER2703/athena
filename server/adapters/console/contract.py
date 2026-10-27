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
# Identité et installation de la Console (spec #75) : publiques, elles servent
# à choisir l'écran avant toute authentification.
SESSION_PATH = "/api/session"
SETUP_PATH = "/api/setup"
LOGIN_PATH = "/api/login"
# Jetons d'accès (spec #75, ticket #78) : authentifiés par le Ticket Console.
# La révocation porte sur un Jeton précis, d'où le segment d'identifiant.
TOKENS_PATH = "/api/tokens"
# Sonde de supervision : publique, réponse fixe, aucun état applicatif.
HEALTH_PATH = "/healthz"
# Nom stable et explicite du cookie qui porte le Ticket Console. Le préfixe
# ``athena_`` évite de le confondre avec un cookie d'un autre service du même
# hôte (le front et l'API partagent l'origine).
TICKET_COOKIE = "athena_ticket"
