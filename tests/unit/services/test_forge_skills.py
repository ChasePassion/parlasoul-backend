"""Skill discovery, rendering and the load_skill guard."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from src.core.tts_voice_catalog import dashscope_full_voice_catalog
from src.services.character_forge_tools import (
    ForgeToolExecutor,
    ToolContext,
    collect_reference_images,
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
