from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.schemas.character import CharacterCreate, CharacterUpdate, VisibilityEnum


def _build_character_create(**overrides):
    payload = {
        "name": "A",
        "description": "B",
        "system_prompt": "C",
        "voice_provider": "dashscope",
        "voice_model": "qwen3-tts-instruct-flash-realtime",
        "voice_provider_voice_id": "Cherry",
        "voice_source_type": "system",
        "visibility": VisibilityEnum.PRIVATE,
    }
    payload.update(overrides)
    return CharacterCreate(**payload)


def test_character_create_rejects_tags_field() -> None:
    with pytest.raises(ValidationError) as exc:
        _build_character_create(tags=["photographer"])

    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_character_update_rejects_tags_field() -> None:
    with pytest.raises(ValidationError) as exc:
        CharacterUpdate(tags=["photographer"])

    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_character_create_accepts_llm_preset() -> None:
    payload = _build_character_create(llm_preset_id="flagship")

    assert payload.llm_preset_id == "flagship"


def test_character_update_accepts_llm_preset() -> None:
    payload = CharacterUpdate(llm_preset_id="free")

    assert payload.llm_preset_id == "free"


def test_character_create_rejects_unknown_llm_preset() -> None:
    with pytest.raises(ValidationError) as exc:
        _build_character_create(llm_preset_id="custom-model")

    assert exc.value.errors()[0]["type"] == "value_error"
    assert exc.value.errors()[0]["loc"] == ("llm_preset_id",)


def test_character_create_rejects_null_llm_preset() -> None:
    with pytest.raises(ValidationError) as exc:
        _build_character_create(llm_preset_id=None)

    assert exc.value.errors()[0]["type"] == "string_type"


def test_character_update_rejects_removed_dialogue_style_field() -> None:
    with pytest.raises(ValidationError) as exc:
        CharacterUpdate(dialogue_style_id="overpowered")

    assert exc.value.errors()[0]["type"] == "extra_forbidden"
