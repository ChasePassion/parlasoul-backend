"""Skill discovery, rendering and the load_skill guard."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from src.core.tts_voice_catalog import dashscope_legacy_voice_catalog
from src.repositories.image_generation_repository import build_image_payload
from src.services.character_forge_prompt import build_forge_system_prompt
from src.services.character_forge_tools import (
    ForgeToolExecutor,
    ToolContext,
    build_tool_schemas,
    collect_reference_images,
)
from src.services.forge_skills import (
    SKILL_NAMES,
    load_forge_skill,
)

_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "services"
    / "forge_skills"
    / "prompt_creation"
)
SYSTEM_PROMPT_TEMPLATE_DAILY = (
    _TEMPLATE_DIR / "system_prompt_template_daily.md"
).read_text(encoding="utf-8")
SYSTEM_PROMPT_TEMPLATE_NARRATIVE = (
    _TEMPLATE_DIR / "system_prompt_template_narrative.md"
).read_text(encoding="utf-8")


def _context() -> ToolContext:
    return ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(active_character_id=None),
    )


def _executor() -> ForgeToolExecutor:
    return ForgeToolExecutor(
        character_service=None,
        chat_service=None,
        exa_repository=None,
        avatar_service=None,
    )




def test_character_skill_renders_both_system_prompt_templates():
    skill = load_forge_skill("prompt-creation")
    assert skill is not None
    assert SYSTEM_PROMPT_TEMPLATE_DAILY in skill.content
    assert SYSTEM_PROMPT_TEMPLATE_NARRATIVE in skill.content
    assert "{{" not in skill.content


def test_voice_skill_renders_the_live_catalog():
    skill = load_forge_skill("voice-creation")
    assert skill is not None
    for item in dashscope_legacy_voice_catalog():
        assert item.provider_voice_id in skill.content
    assert "{{" not in skill.content


def test_unknown_skill_is_not_loadable():
    assert load_forge_skill("does-not-exist") is None


def test_image_payload_uses_agent_mode_only_for_text_to_image():
    text_only = build_image_payload(
        model="qwen-image-3.0",
        size="2048*2048",
        prompt="a portrait",
        reference_images=None,
    )
    assert text_only["input"]["messages"][0]["content"] == [{"text": "a portrait"}]
    assert text_only["parameters"]["prompt_extend_mode"] == "agent"
    assert text_only["parameters"]["enable_thinking"] is True

    with_reference = build_image_payload(
        model="qwen-image-3.0",
        size="2048*2048",
        prompt="a portrait",
        reference_images=["data:image/jpeg;base64,AAAA"],
    )
    assert with_reference["input"]["messages"][0]["content"] == [
        {"image": "data:image/jpeg;base64,AAAA"},
        {"text": "a portrait"},
    ]
    assert "prompt_extend_mode" not in with_reference["parameters"]
    assert "enable_thinking" not in with_reference["parameters"]


def test_collect_reference_images_keeps_only_owned_inline_or_public_images(monkeypatch):
    monkeypatch.setattr("src.core.config.settings.R2_PUBLIC_BASE_URL", None)
    user_id = uuid.uuid4()
    owned = f"images/avatars/chat/{user_id}/one"
    messages = [
        SimpleNamespace(
            role="assistant",
            images=[{"image_key": owned, "data_url": "data:image/jpeg;base64,BBBB"}],
        ),
        SimpleNamespace(
            role="user",
            images=[
                {"image_key": owned, "data_url": "data:image/jpeg;base64,AAAA"},
                {"image_key": f"images/avatars/chat/{uuid.uuid4()}/foreign"},
                {"image_key": f"images/avatars/chat/{user_id}/plain"},
            ],
        ),
    ]

    images = collect_reference_images(messages, user_id=user_id)

    assert images == {owned: "data:image/jpeg;base64,AAAA"}


def test_system_prompt_stays_lean_and_contains_only_the_catalog():
    prompt = build_forge_system_prompt(
        current_date=datetime(2026, 11, 6),
        active_character=None,
    )
    assert len(prompt) < 2000
    assert SYSTEM_PROMPT_TEMPLATE_DAILY not in prompt
    assert SYSTEM_PROMPT_TEMPLATE_NARRATIVE not in prompt
    for name in SKILL_NAMES:
        assert name in prompt


async def test_load_skill_tool_returns_content_and_marks_context():
    executor = _executor()
    context = _context()

    result = await executor.execute(
        "load_skill", {"name": "prompt-creation"}, context
    )

    assert result.llm_result["ok"] is True
    assert result.llm_result["skill"] == "prompt-creation"
    assert SYSTEM_PROMPT_TEMPLATE_DAILY in result.llm_result["content"]
    assert "prompt-creation" in context.loaded_skills


class _FakeCharacterService:
    def __init__(self) -> None:
        self.created: dict[str, object] | None = None

    async def create_character(self, character_in, current_user):
        self.created = {"in": character_in}
        return SimpleNamespace(
            id=str(uuid.uuid4()),
            name=character_in.name,
            description=character_in.description,
            avatar_image_key=character_in.avatar_image_key,
            visibility="PRIVATE",
            voice_provider_voice_id=character_in.voice_provider_voice_id,
            voice_profile=character_in.voice_profile,
        )


class _FakeChatService:
    async def create_chat(self, current_user, character_id, meta):
        return SimpleNamespace(chat=SimpleNamespace(id=uuid.uuid4()))


async def test_create_character_persists_and_returns_created_card():
    characters = _FakeCharacterService()
    executor = ForgeToolExecutor(
        character_service=characters,
        chat_service=_FakeChatService(),
        exa_repository=None,
        avatar_service=None,
    )
    context = ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(active_character_id=None),
        loaded_skills={"voice-creation", "prompt-creation"},
    )
    voice_id = dashscope_legacy_voice_catalog()[0].provider_voice_id
    preview = await executor.execute(
        "preview_voice",
        {
            "base_voice_id": voice_id,
            "speech_rate": 1.0,
            "volume": 50,
            "pitch_rate": 1.0,
            "sample_text": "这是一句用来测试创建角色的试听台词。",
        },
        context,
    )
    assert preview.llm_result["ok"] is True

    created = await executor.execute(
        "create_character",
        {
            "name": "测试角色",
            "description": "一个用于测试创建流的角色",
            "system_prompt": "x" * 300,
            "voice_preview_id": preview.llm_result["preview_id"],
        },
        context,
    )

    assert created.llm_result["ok"] is True
    assert created.ui_payload is not None
    assert created.ui_payload["type"] == "character_created"
    assert created.ui_payload["name"] == "测试角色"
    assert created.ui_payload["chat_id"]
    assert created.character_created == created.ui_payload
    assert characters.created is not None


def test_tool_schema_exposes_available_reference_images():
    schemas = build_tool_schemas(reference_image_keys=["images/avatars/chat/u/one"])
    avatar = next(
        schema["function"]
        for schema in schemas
        if schema["function"]["name"] == "generate_avatar"
    )
    assert avatar["parameters"]["properties"]["reference_image_keys"]["items"]["enum"] == [
        "images/avatars/chat/u/one"
    ]
    assert "reference_image_keys" not in build_tool_schemas()[2]["function"][
        "parameters"
    ]["properties"]


async def test_generate_avatar_passes_resolved_reference_images():
    class _FakeAvatarService:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None

        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return "images/avatars/characters/u/avatar", None

    avatars = _FakeAvatarService()
    executor = ForgeToolExecutor(
        character_service=None,
        chat_service=None,
        exa_repository=None,
        avatar_service=avatars,
    )
    context = ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(active_character_id=None),
        loaded_skills={"avatar-creation"},
        reference_images={
            "images/avatars/chat/u/one": "data:image/jpeg;base64,AAAA",
            "images/avatars/chat/u/two": "https://example.test/two.jpg",
        },
    )

    result = await executor.execute(
        "generate_avatar",
        {
            "prompt": "一张写实半身肖像，主体居中，柔和的自然光线，面部细节清晰。",
            "reference_image_keys": [
                "images/avatars/chat/u/one",
                "images/avatars/chat/u/two",
            ],
        },
        context,
    )

    assert result.llm_result["ok"] is True
    assert avatars.kwargs is not None
    assert avatars.kwargs["reference_images"] == [
        "data:image/jpeg;base64,AAAA",
        "https://example.test/two.jpg",
    ]


async def test_unknown_skill_tool_call_fails():
    result = await _executor().execute(
        "load_skill", {"name": "unknown"}, _context()
    )
    assert result.llm_result["ok"] is False


async def test_tools_require_their_skill_first():
    executor = _executor()
    context = _context()

    voice_error = await executor.execute(
        "preview_voice",
        {
            "base_voice_id": "Maia",
            "speech_rate": 1.0,
            "volume": 50,
            "pitch_rate": 1.0,
            "sample_text": "这是一句用来测试技能门禁的台词。",
        },
        context,
    )
    assert voice_error.llm_result["ok"] is False
    assert "load_skill" in voice_error.llm_result["errors"][0]

    create_error = await executor.execute(
        "create_character", {}, context
    )
    assert create_error.llm_result["ok"] is False
    assert "load_skill" in create_error.llm_result["errors"][0]

    await executor.execute("load_skill", {"name": "voice-creation"}, context)
    retry = await executor.execute(
        "preview_voice",
        {
            "base_voice_id": "not-a-voice",
            "speech_rate": 1.0,
            "volume": 50,
            "pitch_rate": 1.0,
            "sample_text": "这是一句用来测试技能门禁的台词。",
        },
        context,
    )
    assert retry.llm_result["ok"] is False
    assert "load_skill" not in retry.llm_result["errors"][0]
