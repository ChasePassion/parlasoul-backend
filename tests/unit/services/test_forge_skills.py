"""Skill discovery, rendering and the load_skill guard."""

from __future__ import annotations

import re
import uuid
from types import SimpleNamespace

from src.core.tts_voice_catalog import dashscope_full_voice_catalog
from src.repositories.image_generation_repository import build_image_payload
from src.services.character_forge_tools import (
    ForgeToolExecutor,
    ToolContext,
    build_tool_schemas,
    collect_reference_images,
)
from src.services.forge_skills import (
    load_forge_skill,
)


class _FakePreviewStore:
    """In-memory stand-in for the Redis-backed voice preview store."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, object]] = {}

    async def create(self, *, session_id, user_id, payload) -> str:
        handle = f"pv{len(self.items) + 1}"
        self.items[handle] = {
            "session_id": session_id,
            "user_id": user_id,
            "payload": payload,
        }
        return handle

    async def get(self, *, session_id, handle, user_id):
        item = self.items[handle]
        assert item["session_id"] == session_id
        assert item["user_id"] == user_id
        return item["payload"]


def _context() -> ToolContext:
    return ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(id=uuid.uuid4(), active_character_id=None),
    )


def _executor() -> ForgeToolExecutor:
    return ForgeToolExecutor(
        character_service=None,
        chat_service=None,
        search_repository=None,
        preview_store=_FakePreviewStore(),
        avatar_service=None,
        audio_service=None,
        voice_profile_service=None,
    )

def test_voice_skill_renders_the_live_catalog():
    skill = load_forge_skill("voice-creation")
    assert skill is not None
    for item in dashscope_full_voice_catalog():
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
        search_repository=None,
        preview_store=_FakePreviewStore(),
        avatar_service=None,
        audio_service=None,
        voice_profile_service=None,
    )
    context = ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(id=uuid.uuid4(), active_character_id=None),
        loaded_skills={"voice-creation", "prompt-creation"},
    )
    voice_id = dashscope_full_voice_catalog()[0].provider_voice_id
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
    assert preview.llm_result["preview_id"] == "pv1"

    created = await executor.execute(
        "create_character",
        {
            "name": "测试角色",
            "description": "一个用于测试创建流的角色",
            "system_prompt": "x" * 300,
            "voice_base_id": voice_id,
            "voice_speech_rate": 1.0,
            "voice_volume": 50,
            "voice_pitch_rate": 1.0,
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
    assert characters.created["in"].voice_provider_voice_id == voice_id


async def _run_preview(executor, context, voice_id):
    return await executor.execute(
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


def test_character_tools_never_ask_the_model_for_an_opaque_preview_token():
    schemas = build_tool_schemas()
    for tool_name in ("create_character", "update_character"):
        tool = next(
            schema["function"]
            for schema in schemas
            if schema["function"]["name"] == tool_name
        )
        properties = tool["parameters"]["properties"]
        assert "voice_preview_id" not in properties
        assert "voice_base_id" in properties
        assert "voice_speech_rate" in properties


async def test_preview_handle_is_a_short_serial_and_the_audio_url_stays_short():
    executor = _executor()
    context = _context()
    context.loaded_skills.add("voice-creation")
    voice_id = dashscope_full_voice_catalog()[0].provider_voice_id

    preview = await _run_preview(executor, context, voice_id)

    assert re.fullmatch(r"pv\d+", preview.llm_result["preview_id"])
    assert preview.ui_payload is not None
    assert preview.ui_payload["preview_url"].endswith(
        f"/{context.session.id}/{preview.llm_result['preview_id']}/audio"
    )


async def test_update_character_keeps_the_existing_shaping_when_only_the_voice_changes():
    class _FakeUpdatingCharacterService:
        def __init__(self) -> None:
            self.updated: dict[str, object] | None = None

        async def get_character(self, character_id, *, current_user):
            return SimpleNamespace(
                voice_profile={
                    "base_voice_id": "Eldric Sage",
                    "speech_rate": 0.88,
                    "volume": 35,
                    "pitch_rate": 0.95,
                }
            )

        async def update_character(self, character_id, *, character_in, current_user):
            self.updated = {"in": character_in}
            return SimpleNamespace(
                id=str(character_id),
                name=character_in.name or "Li Lu",
                description=character_in.description or "价值投资大师",
                avatar_image_key=None,
                voice_provider_voice_id=character_in.voice_provider_voice_id,
                voice_profile=character_in.voice_profile,
            )

    characters = _FakeUpdatingCharacterService()
    executor = ForgeToolExecutor(
        character_service=characters,
        chat_service=None,
        search_repository=None,
        preview_store=_FakePreviewStore(),
        avatar_service=None,
        audio_service=None,
        voice_profile_service=None,
    )
    context = ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(
            id=uuid.uuid4(), active_character_id=str(uuid.uuid4())
        ),
        loaded_skills={"prompt-creation"},
    )
    other_voice = dashscope_full_voice_catalog()[1].provider_voice_id

    result = await executor.execute(
        "update_character",
        {"voice_base_id": other_voice},
        context,
    )

    assert result.llm_result["ok"] is True
    assert characters.updated is not None
    profile = characters.updated["in"].voice_profile
    assert profile["base_voice_id"] == other_voice
    assert profile["speech_rate"] == 0.88
    assert profile["volume"] == 35
    assert profile["pitch_rate"] == 0.95


async def test_create_character_rejects_a_voice_outside_the_catalog():
    characters = _FakeCharacterService()
    executor = ForgeToolExecutor(
        character_service=characters,
        chat_service=_FakeChatService(),
        search_repository=None,
        preview_store=_FakePreviewStore(),
        avatar_service=None,
        audio_service=None,
        voice_profile_service=None,
    )
    context = ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(id=uuid.uuid4(), active_character_id=None),
        loaded_skills={"prompt-creation"},
    )

    result = await executor.execute(
        "create_character",
        {
            "name": "测试角色",
            "description": "一个用于测试创建流的角色",
            "system_prompt": "x" * 300,
            "voice_base_id": "No Such Voice",
        },
        context,
    )

    assert result.llm_result["ok"] is False
    assert characters.created is None


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
        search_repository=None,
        preview_store=_FakePreviewStore(),
        avatar_service=avatars,
        audio_service=None,
        voice_profile_service=None,
    )
    context = ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(id=uuid.uuid4(), active_character_id=None),
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

    clone_error = await executor.execute(
        "clone_voice",
        {"audio_key": "audio/whatever.wav", "display_name": "My Voice"},
        context,
    )
    assert clone_error.llm_result["ok"] is False
    assert "voice-clone" in clone_error.llm_result["errors"][0]

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
