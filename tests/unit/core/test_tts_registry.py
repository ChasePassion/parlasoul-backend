from __future__ import annotations

import pytest

from src.core.exceptions import APIError
from src.core.tts_registry import (
    TTSRegistry,
)
from src.core.tts_settings import TTSSettings
from src.core.tts_voice_catalog import (
    DEFAULT_MINIMAX_TTS_MODEL,
)


def _registry() -> TTSRegistry:
    return TTSRegistry(
        TTSSettings(
            MINIMAX_TTS_API_KEY="test-key",
            _env_file=None,
        )
    )




def test_minimax_rejects_opus_audio_format() -> None:
    with pytest.raises(APIError) as exc:
        _registry().resolve_audio_profile(
            provider="minimax",
            model=DEFAULT_MINIMAX_TTS_MODEL,
            audio_format="opus",
            stream_required=True,
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "validation_failed"




