from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from src.core.llm_registry import ResolvedLLMRoute
from src.repositories.saved_item_repository import SavedItemRepository
from src.services.chat_learning_service import ChatLearningService


class _StubGateway:
    def __init__(
        self,
        *,
        text_response: str = "",
        json_response: dict | None = None,
        stream_chunks: list[str] | None = None,
        stream_error: Exception | None = None,
    ):
        self.text_response = text_response
        self.json_response = json_response or {}
        self.stream_chunks = stream_chunks or []
        self.stream_error = stream_error
        self.generate_text = AsyncMock(side_effect=self._generate_text)
        self.generate_json = AsyncMock(side_effect=self._generate_json)
        self.stream_text_calls: list[dict[str, object]] = []

    async def _generate_text(self, *, route, system_prompt: str, user_message: str, **kwargs) -> str:
        assert route
        assert system_prompt
        assert user_message is not None
        return self.text_response

    async def _generate_json(
        self,
        *,
        route,
        system_prompt: str,
        user_message: str,
        default: dict,
        **kwargs,
    ) -> dict:
        assert route
        assert system_prompt
        assert user_message is not None
        return self.json_response or default

    async def stream_text(self, *, route, messages, operation: str, route_role: str, **kwargs):
        self.stream_text_calls.append(
            {
                "route": route,
                "messages": messages,
                "operation": operation,
                "route_role": route_role,
                **kwargs,
            }
        )
        if self.stream_error is not None:
            raise self.stream_error
        for chunk in self.stream_chunks:
            yield chunk


class _StubRegistry:
    def __init__(self, route: ResolvedLLMRoute | None = None):
        self._route = route or ResolvedLLMRoute(
            provider="openrouter",
            model="openai/gpt-4.1-mini",
            sdk_kind="pydantic_ai",
            api_key="openrouter-key",
            api_base="https://openrouter.ai/api/v1",
            source="system_learning",
            model_type_label="openrouter:openai/gpt-4.1-mini",
        )
        self.router = type("RouterStub", (), {"learning_provider": self._route.provider})()

    def resolve_learning_route(self) -> ResolvedLLMRoute | None:
        return self._route




@pytest.mark.asyncio
async def test_generate_reply_suggestions_returns_only_valid_model_items() -> None:
    gateway = _StubGateway(json_response={"suggestions": [{"type": "topic", "en": "t", "zh": "话题"}]})
    service = ChatLearningService(gateway_service=gateway, registry=_StubRegistry())

    suggestions = await service.generate_reply_suggestions(
        chat_history=[{"role": "user", "content": "fine"}],
        assistant_latest_message="How are you?",
    )

    assert [item.type for item in suggestions] == ["topic"]
    assert len(suggestions) == 1
    gateway.generate_json.assert_awaited_once()






@pytest.mark.asyncio
async def test_generate_reply_card_ignores_model_favorite_and_reply_level_ipa() -> None:
    gateway = _StubGateway(
        json_response={
            "reply_card": {
                "surface": "Nice to see you.",
                "zh": "见到你很开心。",
                "ipa_us": "/naɪs tu si ju/",
                "key_phrases": [
                    {"surface": "nice to", "zh": "很高兴去", "ipa_us": "/naɪs tə/"},
                    {"surface": "see you", "zh": "见到你", "ipa_us": "/si ju/"},
                ],
                "favorite": {"enabled": True, "is_favorited": False},
            }
        }
    )
    service = ChatLearningService(gateway_service=gateway, registry=_StubRegistry())

    card_core = await service.generate_reply_card(assistant_text="Nice to see you.")

    assert "ipa_us" not in card_core.model_dump()
    assert "favorite" not in card_core.model_dump()
    assert len(card_core.key_phrases) == 2




@pytest.mark.asyncio
async def test_generate_word_card_preserves_selected_surface_and_example() -> None:
    gateway = _StubGateway(
        json_response={
            "word_card": {
                "surface": "rewritten",
                "ipa_us": "/həˈloʊ/",
                "pos_groups": [
                    {
                        "pos": "n.",
                        "senses": [
                            {"zh": "问候", "note": None},
                        ],
                    }
                ],
                "example": {"surface": "Hello there.", "zh": "你好呀。"},
            }
        }
    )
    service = ChatLearningService(gateway_service=gateway, registry=_StubRegistry())

    card_core = await service.generate_word_card(
        selected_text="Hello",
        context_text="Hello there.",
    )

    assert card_core.surface == "Hello"
    assert card_core.example.surface == "Hello there."
    assert card_core.pos_groups[0].senses[0].zh == "问候"




@pytest.mark.asyncio
async def test_generate_feedback_card_allows_single_valid_key_phrase() -> None:
    gateway = _StubGateway(
        json_response={
            "surface": "I really want to go now.",
            "zh": "我现在真的很想走了。",
            "key_phrases": [
                {"surface": "really want to", "ipa_us": "/ˈriəli wɑnt tə/", "zh": "真的想要"},
            ],
        }
    )
    service = ChatLearningService(gateway_service=gateway, registry=_StubRegistry())

    card_core = await service.generate_feedback_card(
        chat_history=[{"role": "assistant", "content": "Hi"}],
        user_message_to_improve="i want go now",
    )

    assert card_core is not None
    assert card_core.surface == "I really want to go now."
    assert len(card_core.key_phrases) == 1


@pytest.mark.asyncio
async def test_load_preferred_expressions_returns_latest_unique_surfaces() -> None:
    saved_repo = SavedItemRepository.__new__(SavedItemRepository)
    saved_repo.list_recent_by_user = AsyncMock(
        return_value=[
            type("SavedRow", (), {"display_surface": "hit it off"})(),
            type("SavedRow", (), {"display_surface": "hit it off"})(),
            type("SavedRow", (), {"display_surface": "catch up"})(),
            type("SavedRow", (), {"display_surface": ""})(),
        ]
    )
    service = ChatLearningService(
        gateway_service=_StubGateway(),
        registry=_StubRegistry(),
        saved_item_repo=saved_repo,
    )

    result = await service.load_preferred_expressions(user_id=uuid.uuid4())

    assert result == ["hit it off", "catch up"]




def _parse_sse_payloads(events: list[str]) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for event in events:
        payloads.append(json.loads(event.removeprefix("data:").strip()))
    return payloads




@pytest.mark.asyncio
async def test_stream_assistant_reply_emits_chunk_and_done_events() -> None:
    gateway = _StubGateway(stream_chunks=["Hello", " world"])
    service = ChatLearningService(gateway_service=gateway, registry=_StubRegistry())

    events = [
        event
        async for event in service.stream_assistant_reply(
            question="Explain this",
            character_chat_context=[
                {"role": "assistant", "content": "How was your weekend?"},
                {"role": "user", "content": "It was great."},
            ],
            assistant_chat_context=[
                {"role": "user", "content": "What does that mean?"},
                {"role": "assistant", "content": "It means it sounds pleasant."},
            ],
            user_id="user-1",
            chat_id="chat-1",
        )
    ]
    payloads = _parse_sse_payloads(events)

    assert payloads == [
        {"type": "chunk", "content": "Hello"},
        {"type": "chunk", "content": " world"},
        {"type": "done", "full_content": "Hello world"},
    ]
    assert gateway.stream_text_calls[0]["operation"] == "learning.assistant.answer"
    assert gateway.stream_text_calls[0]["route_role"] == "learning"
    assert gateway.stream_text_calls[0]["user_id"] == "user-1"
    assert gateway.stream_text_calls[0]["chat_id"] == "chat-1"


@pytest.mark.asyncio
async def test_stream_assistant_reply_emits_error_event_on_gateway_failure() -> None:
    gateway = _StubGateway(stream_error=RuntimeError("boom"))
    service = ChatLearningService(gateway_service=gateway, registry=_StubRegistry())

    events = [
        event
        async for event in service.stream_assistant_reply(
            question="Explain this",
            character_chat_context=[],
            assistant_chat_context=[],
        )
    ]
    payloads = _parse_sse_payloads(events)

    assert len(payloads) == 1
    assert payloads[0]["type"] == "error"
    assert payloads[0]["code"] == "llm_service_error"
    assert "boom" not in payloads[0]["message"]
