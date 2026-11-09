"""clone_voice must forward every required argument to the voice profile service."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from src.services.character_forge_tools import ForgeToolExecutor, ToolContext
from src.services.media_url_service import chat_audio_key


class _FakeAudioService:
    async def load_chat_audio(self, audio_key: str) -> bytes:
        return b"RIFFfake"

    async def probe_info(self, body: bytes, fmt: str) -> SimpleNamespace:
        return SimpleNamespace(duration_seconds=12.5)


class _FakeVoiceProfiles:
    CLONE_VOICE_ID = "cosyvoice-v3-flash-clone-1"

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    async def create_clone(self, **kwargs: Any) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(
            voice=SimpleNamespace(
                id=uuid.uuid4(),
                display_name=kwargs["display_name"],
                preview_text=kwargs["preview_text"],
            )
        )

    async def get_voice_by_id(self, *, voice_id: Any, current_user: Any) -> SimpleNamespace:
        return SimpleNamespace(
            provider_voice_id=self.CLONE_VOICE_ID,
            provider="dashscope",
            provider_model="cosyvoice-v3-flash",
        )


class _FakeCharacterService:
    def __init__(self) -> None:
        self.created: Any = None

    async def create_character(self, payload: Any, user: Any) -> SimpleNamespace:
        self.created = payload
        return SimpleNamespace(
            id=str(uuid.uuid4()),
            name=payload.name,
            description=payload.description,
            avatar_image_key=payload.avatar_image_key,
            visibility="private",
            voice_profile=payload.voice_profile,
        )


class _FakeChatService:
    async def create_chat(self, user: Any, character_id: Any, _: Any) -> SimpleNamespace:
        return SimpleNamespace(chat=SimpleNamespace(id=uuid.uuid4()))


def _executor(
    voices: _FakeVoiceProfiles,
    *,
    characters: _FakeCharacterService | None = None,
) -> ForgeToolExecutor:
    return ForgeToolExecutor(
        character_service=characters,  # type: ignore[arg-type]
        chat_service=_FakeChatService(),  # type: ignore[arg-type]
        search_repository=None,
        preview_store=None,
        avatar_service=None,
        audio_service=_FakeAudioService(),
        voice_profile_service=voices,
    )


async def test_clone_voice_forwards_required_arguments() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    context = ToolContext(
        user=user,
        session=SimpleNamespace(id=uuid.uuid4(), active_character_id=None),
        loaded_skills={"voice-clone"},
    )
    voices = _FakeVoiceProfiles()

    outcome = await _executor(voices).execute(
        "clone_voice",
        {
            "audio_key": chat_audio_key(str(user.id), "sample.wav"),
            "display_name": "Warm Narrator",
        },
        context,
    )

    assert outcome.llm_result["ok"] is True
    assert voices.kwargs is not None
    assert voices.kwargs["provider"] == "dashscope"
    assert voices.kwargs["description"] is None
    assert voices.kwargs["idempotency_key"] is None
    assert voices.kwargs["source_audio_format"] == "wav"
    assert outcome.ui_payload is not None
    assert outcome.ui_payload["type"] == "voice_cloned"


async def test_create_character_accepts_a_cloned_voice() -> None:
    user = SimpleNamespace(id=uuid.uuid4())
    context = ToolContext(
        user=user,
        session=SimpleNamespace(id=uuid.uuid4(), active_character_id=None),
        loaded_skills={"prompt-creation"},
    )
    voices = _FakeVoiceProfiles()
    characters = _FakeCharacterService()

    outcome = await _executor(voices, characters=characters).execute(
        "create_character",
        {
            "name": "Emma",
            "description": "A patient English speaking coach",
            "system_prompt": "You are Emma, a patient English speaking coach. " * 6,
            "voice_profile_id": str(uuid.uuid4()),
        },
        context,
    )

    assert outcome.llm_result["ok"] is True
    assert characters.created is not None
    assert characters.created.voice_model == "cosyvoice-v3-flash"
    assert characters.created.voice_source_type == "clone"
    assert outcome.ui_payload is not None
    assert outcome.ui_payload["voice"]["provider_voice_id"] == (
        _FakeVoiceProfiles.CLONE_VOICE_ID
    )
