"""Realtime session state definitions."""

from __future__ import annotations

from enum import StrEnum


class RealtimeSessionState(StrEnum):
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    ENDPOINT_PENDING = "endpoint_pending"
    THINKING = "thinking"
    AGENT_SPEAKING = "agent_speaking"
    INTERRUPTING = "interrupting"

    # Compatibility aliases for callers which have not migrated to the
    # explicit turn-taking names yet. Internal realtime code uses the names
    # above so there is only one state vocabulary.
    IDLE = "listening"
    AI_RESPONDING = "agent_speaking"
    INTERRUPTED = "interrupting"
