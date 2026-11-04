from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.realtime.schemas.client_events import (
    InputAudioBufferMuteEvent,
    InputAudioBufferUnmuteEvent,
    ResponseCancelEvent,
    SessionUpdateEvent,
)


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


def test_response_cancel_event_accepts_type_only() -> None:
    event = ResponseCancelEvent.model_validate(
        {
            "type": "response.cancel",
        }
    )

    assert event.type == "response.cancel"


def test_input_audio_buffer_mute_event_accepts_type_only() -> None:
    event = InputAudioBufferMuteEvent.model_validate(
        {
            "type": "input_audio_buffer.mute",
        }
    )

    assert event.type == "input_audio_buffer.mute"


def test_input_audio_buffer_unmute_event_accepts_type_only() -> None:
    event = InputAudioBufferUnmuteEvent.model_validate(
        {
            "type": "input_audio_buffer.unmute",
        }
    )

    assert event.type == "input_audio_buffer.unmute"
