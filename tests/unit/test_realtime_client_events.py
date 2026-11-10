from __future__ import annotations

from src.realtime.schemas.client_events import SessionUpdateEvent


def test_session_update_event_accepts_translation_flag_only() -> None:
    event = SessionUpdateEvent.model_validate(
        {
            "type": "session.update",
            "translation_enabled": True,
        }
    )

    assert event.translation_enabled is True


