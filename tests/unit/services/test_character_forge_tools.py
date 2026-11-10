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


def _context() -> ToolContext:
    return ToolContext(
        user=SimpleNamespace(id=uuid.uuid4()),
        session=SimpleNamespace(id=uuid.uuid4(), active_character_id=None),
    )


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
    context = _context()

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
    context = _context()

    outcome = await executor.execute(
        "generate_avatar", {"prompt": _AVATAR_PROMPT}, context
    )

    model_facing = outcome.llm_result["model_content"]
    assert "DataInspectionFailed" in model_facing
    assert "Green net check rejected image (input)" in model_facing


async def test_generate_avatar_without_upstream_detail_keeps_the_user_message():
    class _PlainFailure:
        async def generate(
            self, *, user_id: Any, prompt: str, reference_images: list[str] | None
        ) -> tuple[str, Any]:
            raise APIError(
                status_code=502,
                code="external_error",
                message="upstream error: image generation failed; upstream_status=400",
            )

    executor = _executor(avatar_service=_PlainFailure())
    context = _context()

    outcome = await executor.execute(
        "generate_avatar", {"prompt": _AVATAR_PROMPT}, context
    )

    result = outcome.llm_result
    assert result["errors"] == [
        "upstream error: image generation failed; upstream_status=400"
    ]
    assert "upstream error: image generation failed" in result["model_content"]


async def test_search_web_keeps_the_user_facing_message_unchanged():
    executor = _executor(
        search_repository=_RefusingSearchRepository(
            upstream_code="invalid_request_error",
            upstream_message="failed to fetch URL",
        )
    )
    context = _context()

    outcome = await executor.execute("search_web", {"query": "李录"}, context)

    result = outcome.llm_result
    assert result["ok"] is False
    assert "failed to fetch URL" not in result["errors"][0]
    assert "failed to fetch URL" not in _summary(result, "search_web")


async def test_search_web_gives_the_model_the_upstream_reason():
    executor = _executor(
        search_repository=_RefusingSearchRepository(
            upstream_code="invalid_request_error",
            upstream_message="failed to fetch URL",
        )
    )
    context = _context()

    outcome = await executor.execute("search_web", {"query": "李录"}, context)

    model_facing = outcome.llm_result["model_content"]
    assert "invalid_request_error" in model_facing
    assert "failed to fetch URL" in model_facing


async def test_generate_avatar_can_be_called_repeatedly():
    """No per-turn cap: the agent may retry the avatar as often as it needs."""

    class _CountingAvatarService:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(
            self, *, user_id: Any, prompt: str, reference_images: list[str] | None
        ) -> tuple[str, Any]:
            self.calls += 1
            return f"images/avatars/characters/{user_id}/key{self.calls}", None

    avatar = _CountingAvatarService()
    executor = _executor(avatar_service=avatar)
    context = _context()

    for _ in range(5):
        outcome = await executor.execute(
            "generate_avatar", {"prompt": _AVATAR_PROMPT}, context
        )
        assert outcome.llm_result["ok"] is True

    assert avatar.calls == 5


async def test_search_web_can_be_called_repeatedly():
    """No per-turn cap on searching, and fetching does not share that budget."""

    class _CountingSearchRepository:
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, *, query: str, num_results: int) -> list[Any]:
            self.calls += 1
            return []

        async def fetch_page(self, *, url: str) -> dict[str, Any]:
            self.calls += 1
            return {"url": url, "title": "标题", "text": "正文"}

    search = _CountingSearchRepository()
    executor = _executor(search_repository=search)
    context = _context()

    for _ in range(5):
        outcome = await executor.execute("search_web", {"query": "李录"}, context)
        assert outcome.llm_result["ok"] is True

    for _ in range(5):
        outcome = await executor.execute(
            "read_webpage", {"url": "https://example.com/a"}, context
        )
        assert outcome.llm_result["ok"] is True

    assert search.calls == 10
