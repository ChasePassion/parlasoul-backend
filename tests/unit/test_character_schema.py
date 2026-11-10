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


@pytest.mark.parametrize(
    ("build", "payload"),
    [
        (_build_character_create, {"tags": ["photographer"]}),
        (CharacterUpdate, {"tags": ["photographer"]}),
        (CharacterUpdate, {"dialogue_style_id": "overpowered"}),
    ],
    ids=["create_tags", "update_tags", "update_dialogue_style"],
)
def test_character_schema_rejects_removed_fields(build, payload) -> None:
    with pytest.raises(ValidationError) as exc:
        build(**payload)

    assert exc.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    ("build", "payload", "expected"),
    [
        (_build_character_create, {"llm_preset_id": "flagship"}, "flagship"),
        (CharacterUpdate, {"llm_preset_id": "free"}, "free"),
    ],
    ids=["create", "update"],
)
def test_character_schema_accepts_llm_preset(build, payload, expected) -> None:
    assert build(**payload).llm_preset_id == expected


def test_character_create_rejects_unknown_llm_preset() -> None:
    with pytest.raises(ValidationError) as exc:
        _build_character_create(llm_preset_id="custom-model")

    assert exc.value.errors()[0]["type"] == "value_error"
    assert exc.value.errors()[0]["loc"] == ("llm_preset_id",)


def test_character_create_rejects_null_llm_preset() -> None:
    with pytest.raises(ValidationError) as exc:
        _build_character_create(llm_preset_id=None)

    assert exc.value.errors()[0]["type"] == "string_type"
