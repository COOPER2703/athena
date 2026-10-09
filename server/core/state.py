from __future__ import annotations

from enum import StrEnum


class SessionState(StrEnum):
    IDLE = "Idle"
    LIVE_CONNECTING = "LiveConnecting"
    LIVE_ACTIVE = "LiveActive"
    SESSION_VISIBLE = "SessionVisible"
    CLOSING = "Closing"
