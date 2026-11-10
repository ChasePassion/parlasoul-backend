"""Forge tool errors: the upstream reason reaches the model, not the user."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from src.core.exceptions import APIError
from src.services.character_forge_service import CharacterForgeService
from src.services.character_forge_tools import ForgeToolExecutor, ToolContext

_AVATAR_PROMPT = "一位年长男性投资人的半身肖像，深藏青西装，细边方框眼镜"
_AVATAR_API_MESSAGE = (
    "upstream error: image generation failed; upstream_status=400; "
    "upstream_code=DataInspectionFailed"
)


class _RefusingAvatarService:
    """Avatar service that fails the way DashScope refuses a moderated image."""

    def __init__(self, *, upstream_code: str, upstream_message: str) -> None:
        self._upstream_code = upstream_code
        self._upstream_message = upstream_message

    async def generate(
        self, *, user_id: Any, prompt: str, reference_images: list[str] | None
    ) -> tuple[str, Any]:
        raise APIError(
            status_code=502,
            code="external_error",
            message=_AVATAR_API_MESSAGE,
            upstream_code=self._upstream_code,
            upstream_message=self._upstream_message,
        )


class _RefusingSearchRepository:
    """Search repository whose upstream refuses one specific request."""

    def __init__(self, *, upstream_code: str, upstream_message: str) -> None:
        self._upstream_code = upstream_code
        self._upstream_message = upstream_message

    async def search(self, *, query: str, num_results: int) -> list[Any]:
        raise APIError(
            status_code=502,
            code="external_error",
            message="upstream error: deepseek search failed; upstream_status=404",
            upstream_code=self._upstream_code,
            upstream_message=self._upstream_message,
        )


def _context(*, skills: tuple[str, ...] = ()) -> ToolContext:
    context = ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(id=uuid.uuid4(), active_character_id=None),
    )
    for skill in skills:
        context.loaded_skills.add(skill)
    return context


def _executor(*, avatar_service: Any = None, search_repository: Any = None):
    return ForgeToolExecutor(
        character_service=None,
        chat_service=None,
        search_repository=search_repository,
        preview_store=None,
        avatar_service=avatar_service,
        audio_service=None,
        voice_profile_service=None,
    )


def _summary(result: dict[str, Any], tool: str) -> str:
    """The text the user sees in the tool chip."""
    return CharacterForgeService._tool_summary(tool, result)


async def test_generate_avatar_keeps_the_user_facing_message_unchanged():
    executor = _executor(
        avatar_service=_RefusingAvatarService(
            upstream_code="DataInspectionFailed",
            upstream_message="Green net check rejected image (input)",
        )
    )
    context = _context(skills=("avatar-creation",))

    outcome = await executor.execute(
        "generate_avatar", {"prompt": _AVATAR_PROMPT}, context
    )

    result = outcome.llm_result
    assert result["ok"] is False
    assert result["errors"] == [_AVATAR_API_MESSAGE]
    assert "DataInspectionFailed" not in _summary(result, "generate_avatar")


async def test_generate_avatar_gives_the_model_the_upstream_reason():
    executor = _executor(
        avatar_service=_RefusingAvatarService(
            upstream_code="DataInspectionFailed",
            upstream_message="Green net check rejected image (input)",
        )
    )
    context = _context(skills=("avatar-creation",))

    outcome = await executor.execute(
        "generate_avatar", {"prompt": _AVATAR_PROMPT}, context
    )

    model_facing = outcome.llm_result["model_content"]
    assert "DataInspectionFailed" in model_facing
    assert "Green net check rejected image (input)" in model_facing


