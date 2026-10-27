from __future__ import annotations

from server.adapters.console.adapter import ConsoleAdapter
from server.adapters.console.contract import (
    API_PREFIX,
    ENVELOPE_VERSION,
    EVENTS_PATH,
    HEALTH_PATH,
    LOGIN_PATH,
    SESSION_PATH,
    SETUP_PATH,
    STATUS_PATH,
    STREAM_PATH,
    TICKET_COOKIE,
)

__all__ = [
    "API_PREFIX",
    "ENVELOPE_VERSION",
    "EVENTS_PATH",
    "HEALTH_PATH",
    "LOGIN_PATH",
    "SESSION_PATH",
    "SETUP_PATH",
    "STATUS_PATH",
    "STREAM_PATH",
    "TICKET_COOKIE",
    "ConsoleAdapter",
]
