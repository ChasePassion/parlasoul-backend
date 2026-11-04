from __future__ import annotations

import pytest

from src.core.exceptions import APIError
from src.core.tts_registry import (
    DEFAULT_SYSTEM_TTS_MODEL,
    DEFAULT_SYSTEM_TTS_PROVIDER,
    DEFAULT_SYSTEM_TTS_VOICE_ID,
    TTSRegistry,
)
from src.core.tts_settings import TTSSettings
from src.core.tts_voice_catalog import (
    DEFAULT_MINIMAX_TTS_MODEL,
    legacy_dashscope_voice_id_for_minimax,
)


def _registry() -> TTSRegistry:
    return TTSRegistry(
        TTSSettings(
            MINIMAX_TTS_API_KEY="test-key",
            _env_file=None,
        )
    )


def test_default_system_voice_binding_uses_dashscope() -> None:
    binding = _registry().default_system_voice_binding()

    assert binding.provider == "dashscope"
    assert binding.provider == DEFAULT_SYSTEM_TTS_PROVIDER
    assert binding.model == DEFAULT_SYSTEM_TTS_MODEL
    assert binding.provider_voice_id == DEFAULT_SYSTEM_TTS_VOICE_ID
    assert binding.provider_voice_id == "Cherry"
    assert binding.source_type == "system"


def test_minimax_catalog_keeps_legacy_display_but_uses_minimax_voice_ids() -> None:
    voices = _registry().list_system_voices(provider="minimax", model=None)

    assert len(voices) == 19
    first = voices[0]
    assert first["provider"] == "minimax"
    assert first["display_name"] == "芊悦"
    assert first["provider_voice_id"] == "Chinese (Mandarin)_Wise_Women"
    assert first["preview_audio_url"] == (
        "/media/voices/preview/minimax/Chinese_Mandarin_Wise_Women.mp3"
    )
    assert all(item["display_name"] != "顽屁小孩" for item in voices)
    assert all(item["display_name"] != "燕铮莺" for item in voices)
    assert all(item["display_name"] != "阿闻" for item in voices)
    assert all(item["display_name"] != "徐大爷" for item in voices)
    assert all(item["display_name"] != "沙小弥" for item in voices)


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


def test_explicit_route_rejects_invalid_source_type_with_validation_error() -> None:
    with pytest.raises(APIError) as exc:
        _registry().resolve_explicit_route(
            provider="minimax",
            model=None,
            source_type="unknown",
        )

    assert exc.value.status_code == 422
    assert exc.value.code == "validation_failed"


def test_dashscope_legacy_catalog_keeps_removed_voices_hidden() -> None:
    voices = _registry().list_system_voices(provider="dashscope", model=None)

    assert len(voices) == 19
    assert all(item["display_name"] != "顽屁小孩" for item in voices)
    assert all(item["display_name"] != "燕铮莺" for item in voices)
    assert all(item["display_name"] != "阿闻" for item in voices)
    assert all(item["display_name"] != "徐大爷" for item in voices)
    assert all(item["display_name"] != "沙小弥" for item in voices)


def test_minimax_voice_can_map_back_to_dashscope_for_realtime_voice_calls() -> None:
    assert legacy_dashscope_voice_id_for_minimax("Chinese_Mandarin_Warm_Girl") == "Bella"


def test_dashscope_clone_model_remains_resolvable() -> None:
    route = _registry().resolve_explicit_route(
        provider="dashscope",
        model="qwen3-tts-vc-realtime-2026-01-15",
        source_type="clone",
    )

    assert route.provider == "dashscope"
    assert route.model == "qwen3-tts-vc-realtime-2026-01-15"
    assert route.source_type == "clone"
