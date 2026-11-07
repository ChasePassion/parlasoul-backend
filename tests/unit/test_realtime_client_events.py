from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.realtime.schemas.client_events import SessionUpdateEvent


def test_session_update_event_accepts_translation_flag_only() -> None:
    event = SessionUpdateEvent.model_validate(
        {
            "type": "session.update",
            "translation_enabled": True,
        }
    )

    assert event.translation_enabled is True


def test_session_update_event_rejects_removed_auto_translate_input_flag() -> None:
    with pytest.raises(ValidationError):
        SessionUpdateEvent.model_validate(
            {
                "type": "session.update",
                "auto_translate_input_enabled": True,
            }
        )






