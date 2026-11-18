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
# Déconnexion du navigateur courant, déconnexion de tous les navigateurs du
# Compte, et changement de mot de passe (spec #75, ticket #77).
LOGOUT_PATH = "/api/logout"
LOGOUT_ALL_PATH = "/api/logout-all"
PASSWORD_PATH = "/api/password"
# Réglages et Secrets (ADR-0025) : lecture des noms seulement, écriture et
# effacement par nom. La Conversation est refusée côté serveur tant que
# l'Installation n'est pas terminée (ADR-0026).
SETTINGS_PATH = "/api/settings"
SECRETS_PATH = "/api/secrets"
PLUGINS_PATH = "/api/plugins"
CHAT_PATH = "/api/chat"
# Conversation vocale (ADR-0027) : WebSocket binaire montant (PCM 16 kHz) et
# JSON descendant, authentifiée par le seul Ticket Console. Elle héberge un
# Client Console éphémère, distinct du transport des Clients.
VOICE_PATH = "/api/voice"
# Fil de la Conversation (ADR-0024) : lecture paginée du Journal, marquée par
# l'origine de chaque tour. Aucune écriture, aucun état du noyau.
THREAD_PATH = "/api/thread"
# Sonde de supervision : publique, réponse fixe, aucun état applicatif.
HEALTH_PATH = "/healthz"
# Nom stable et explicite du cookie qui porte le Ticket Console. Le préfixe
# ``athena_`` évite de le confondre avec un cookie d'un autre service du même
# hôte (le front et l'API partagent l'origine).
TICKET_COOKIE = "athena_ticket"
