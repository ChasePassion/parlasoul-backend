"""LLM-backed helpers for chat learning features."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from src.core.error_codes import ErrorCode
from src.core.llm_registry import LLMRegistry, ResolvedLLMRoute, llm_registry
from src.core.logging_context import get_request_id
from src.repositories.saved_item_repository import SavedItemRepository
from src.schemas.learning import (
    CardFavoriteState,
    ChatContextMessage,
    FeedbackCard,
    FeedbackCardCore,
    ReplyCard,
    ReplyCardCore,
    ReplyCardFavorite,
    ReplyCardPhrase,
    ReplySuggestion,
    WordCard,
    WordCardCore,
    WordCardExample,
    WordCardPosGroup,
    WordCardSense,
)
from src.services.llm_gateway_service import LLMGatewayService
from src.services.prompts import (
    FEEDBACK_CARD_PROMPT,
    REPLY_CARD_PROMPT,
    REPLY_SUGGESTIONS_PROMPT,
    TRANSLATE_MIXED_TO_ENGLISH_PROMPT,
    WORD_CARD_PROMPT,
)

_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
_ENGLISH_RE = re.compile(r"[A-Za-z]")
_SUGGESTION_ORDER = ("relationship", "topic", "interaction")
_INVALID_PHRASE_IPA_VALUES = {"", "...", "/.../", "n/a"}
_PREFERRED_EXPRESSIONS_LIMIT = 100
_ASSISTANT_STREAM_ERROR_MESSAGE = "learning assistant request failed"
_CHARACTER_CHAT_CONTEXT_PREFIX = "下面是用户和角色对话的聊天记录（注意并不是和你对话）："
_ASSISTANT_CHAT_CONTEXT_PREFIX = "下面是你和用户对话的聊天记录："
_LEARNING_ASSISTANT_PROMPT = """You are an English learning assistant embedded in a language-learning chat app.

The user is practicing English by conversing with an AI character. The provided chat context may contain:
- the recent conversation between the user and the character
- previous follow-up questions and answers from this learning assistant session

Use all of that context to answer the latest user question.

Your job:
- Help the user understand grammar, vocabulary, idioms, tone, and phrasing in the conversation or in the question.
- Explain why certain wording is used and suggest better alternatives when helpful.
- When explaining English words, always include the American IPA transcription (e.g. **phenomenon** /fəˈnɑːmɪnɑːn/).
- Keep the answer concise, practical, and easy to apply.
- Use short examples only when they make the explanation clearer.
- Reply in Chinese when the user asks in Chinese; reply in English when the user asks in English.
"""

logger = logging.getLogger(__name__)


def _sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class ChatLearningService:
    """Generate translation, suggestions, reply cards, word cards, and feedback cards."""

    def __init__(
        self,
        gateway_service: LLMGatewayService,
        registry: LLMRegistry | None = None,
        saved_item_repo: SavedItemRepository | None = None,
    ):
        self._gateway_service = gateway_service
        self._registry = registry or llm_registry
        self._saved_item_repo = saved_item_repo

    def _learning_route(self):
        route = self._registry.resolve_learning_route()
        if route is None or not route.api_key:
            raise RuntimeError(
                f"{self._registry.router.learning_provider.upper()} learning llm route is not configured"
            )
        return route

    async def _llm_text(
        self,
        *,
        system_prompt: str,
        user_message: str,
        operation: str,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
    ) -> str:
        return await self._gateway_service.generate_text(
            route=self._learning_route(),
            system_prompt=system_prompt,
            user_message=user_message,
            operation=operation,
            route_role="learning",
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
        )

    async def _llm_json(
        self,
        *,
        system_prompt: str,
        user_message: str,
        default: dict[str, Any],
        operation: str,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
        route: ResolvedLLMRoute | None = None,
    ) -> dict[str, Any]:
        return await self._gateway_service.generate_json(
            route=route or self._learning_route(),
            system_prompt=system_prompt,
            user_message=user_message,
            default=default,
            operation=operation,
            route_role="learning",
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
        )

    @staticmethod
    def _sanitize_assistant_context(
        chat_context: list[ChatContextMessage | dict[str, str]] | None,
        *,
        max_messages: int | None = None,
    ) -> list[dict[str, str]]:
        if not chat_context:
            return []

        normalized: list[dict[str, str]] = []

        for raw_item in reversed(chat_context):
            if max_messages is not None and max_messages > 0 and len(normalized) >= max_messages:
                break

            if isinstance(raw_item, ChatContextMessage):
                role = raw_item.role
                content = raw_item.content
            else:
                role = str(raw_item.get("role") or "").strip().lower()
                content = str(raw_item.get("content") or "")

            if role not in {"user", "assistant"}:
                continue

            normalized_content = content.strip()
            if not normalized_content:
                continue

            normalized.append({"role": role, "content": normalized_content})

        normalized.reverse()
        return normalized

    def _build_assistant_messages(
        self,
        *,
        question: str,
        character_chat_context: list[ChatContextMessage | dict[str, str]] | None = None,
        assistant_chat_context: list[ChatContextMessage | dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        normalized_character_context = self._sanitize_assistant_context(character_chat_context)
        normalized_assistant_context = self._sanitize_assistant_context(assistant_chat_context)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _LEARNING_ASSISTANT_PROMPT.strip()},
        ]
        if normalized_character_context:
            messages.append({"role": "system", "content": _CHARACTER_CHAT_CONTEXT_PREFIX})
            messages.extend(normalized_character_context)
        if normalized_assistant_context:
            messages.append({"role": "system", "content": _ASSISTANT_CHAT_CONTEXT_PREFIX})
            messages.extend(normalized_assistant_context)
        messages.append({"role": "user", "content": question.strip()})
        return messages

    async def stream_assistant_reply(
        self,
        *,
        question: str,
        character_chat_context: list[ChatContextMessage | dict[str, str]] | None = None,
        assistant_chat_context: list[ChatContextMessage | dict[str, str]] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
    ) -> AsyncGenerator[str, None]:
        messages = self._build_assistant_messages(
            question=question,
            character_chat_context=character_chat_context,
            assistant_chat_context=assistant_chat_context,
        )
        accumulated: list[str] = []

        try:
            async for chunk in self._gateway_service.stream_text(
                route=self._learning_route(),
                messages=messages,
                operation="learning.assistant.answer",
                route_role="learning",
                user_id=user_id,
                chat_id=chat_id,
            ):
                text = chunk or ""
                if not text:
                    continue
                accumulated.append(text)
                yield _sse_event({"type": "chunk", "content": text})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Learning assistant stream failed; user_id=%s chat_id=%s",
                user_id,
                chat_id,
            )
            frame = {
                "type": "error",
                "code": ErrorCode.LLM_SERVICE_ERROR,
                "message": _ASSISTANT_STREAM_ERROR_MESSAGE,
            }
            request_id = get_request_id()
            if request_id:
                frame["request_id"] = request_id
            yield _sse_event(frame)
            return

        yield _sse_event(
            {
                "type": "done",
                "full_content": "".join(accumulated),
            }
        )

    @staticmethod
    def contains_chinese(text: str) -> bool:
        return bool(_CHINESE_RE.search(text or ""))

    @staticmethod
    def contains_english(text: str) -> bool:
        return bool(_ENGLISH_RE.search(text or ""))

    @staticmethod
    def build_preferred_expressions_prompt(preferred_expressions: list[str] | None) -> str:
        if not preferred_expressions:
            return ""

        cleaned: list[str] = []
        seen: set[str] = set()
        for item in preferred_expressions:
            value = (item or "").strip()
            if not value or value in seen:
                continue
            cleaned.append(value)
            seen.add(value)
            if len(cleaned) >= _PREFERRED_EXPRESSIONS_LIMIT:
                break

        if not cleaned:
            return ""

        lines = [
            "Preferred expressions from the user's recent favorites (use only when natural):",
        ]
        for index, item in enumerate(cleaned, 1):
            lines.append(f"{index}. {item}")
        lines.extend(
            [
                "",
                "Rules:",
                "- Prefer these expressions only when they fit the situation naturally.",
                "- Do not force awkward wording just to use them.",
                "- Do not mention that you were given these expressions.",
            ]
        )
        return "\n".join(lines)

    async def translate_mixed_to_english(
        self,
        content: str,
        *,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        character_id: str | None = None,
    ) -> str:
        response = await self._llm_text(
            system_prompt=TRANSLATE_MIXED_TO_ENGLISH_PROMPT.strip(),
            user_message=content,
            operation="learning.translate.mixed_to_english",
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            character_id=character_id,
        )
        cleaned = (response or "").strip()
        return cleaned or content

    async def load_preferred_expressions(
        self,
        *,
        user_id: uuid.UUID,
        limit: int = _PREFERRED_EXPRESSIONS_LIMIT,
    ) -> list[str]:
        if self._saved_item_repo is None or limit <= 0:
            return []

        rows = await self._saved_item_repo.list_recent_by_user(
            user_id=user_id,
            limit=min(limit, _PREFERRED_EXPRESSIONS_LIMIT),
        )
        expressions: list[str] = []
        seen: set[str] = set()
        for row in rows:
            surface = (row.display_surface or "").strip()
            if not surface or surface in seen:
                continue
            expressions.append(surface)
            seen.add(surface)
        return expressions

    async def resolve_favorite_state(
        self,
        *,
        kind: str,
        surface: str,
        user_id: uuid.UUID,
    ) -> CardFavoriteState:
        normalized_surface = (surface or "").strip()
        if self._saved_item_repo is None or not normalized_surface:
            return CardFavoriteState(enabled=True, is_favorited=False, saved_item_id=None)

        item = await self._saved_item_repo.find_latest_by_surface(
            user_id=user_id,
            kind=kind,
            surface=normalized_surface,
        )
        if item is None:
            return CardFavoriteState(enabled=True, is_favorited=False, saved_item_id=None)

        return CardFavoriteState(
            enabled=True,
            is_favorited=True,
            saved_item_id=str(item.id),
        )

    async def generate_reply_suggestions(
        self,
        *,
        chat_history: list[dict[str, str]],
        assistant_latest_message: str,
        preferred_expressions: list[str] | None = None,
        route: ResolvedLLMRoute | None = None,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
    ) -> list[ReplySuggestion]:
        payload = {
            "chat_history": chat_history,
            "assistant_latest_message": assistant_latest_message,
        }
        system_prompt = REPLY_SUGGESTIONS_PROMPT.strip()
        preferred_prompt = self.build_preferred_expressions_prompt(preferred_expressions)
        if preferred_prompt:
            system_prompt = f"{system_prompt}\n\n{preferred_prompt}"

        raw = await self._llm_json(
            system_prompt=system_prompt,
            user_message=json.dumps(payload, ensure_ascii=False),
            default={"suggestions": []},
            operation="learning.reply_suggestions.generate",
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
            route=route,
        )
        items = raw.get("suggestions")
        parsed: list[ReplySuggestion] = []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    parsed.append(
                        ReplySuggestion(
                            type=item.get("type", ""),
                            en=(item.get("en") or "").strip(),
                            zh=(item.get("zh") or "").strip(),
                        )
                    )
                except Exception:
                    continue

        by_type = {
            item.type: item
            for item in parsed
            if item.type in _SUGGESTION_ORDER and item.en and item.zh
        }
        ordered: list[ReplySuggestion] = []
        for suggestion_type in _SUGGESTION_ORDER:
            if suggestion_type in by_type:
                ordered.append(by_type[suggestion_type])
        return ordered

    async def generate_reply_card(
        self,
        *,
        assistant_text: str,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
    ) -> ReplyCardCore | None:
        payload = {"assistant_text": assistant_text}
        raw = await self._llm_json(
            system_prompt=REPLY_CARD_PROMPT.strip(),
            user_message=json.dumps(payload, ensure_ascii=False),
            default={"reply_card": {}},
            operation="learning.reply_card.generate",
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
        )
        card_data = raw.get("reply_card")
        if not isinstance(card_data, dict):
            return None

        try:
            surface = (card_data.get("surface") or "").strip() or (assistant_text or "").strip()
            zh = (card_data.get("zh") or "").strip()
            key_phrases = self._parse_reply_card_phrases(card_data.get("key_phrases"))[:4]
            if not surface or not zh or len(key_phrases) < 2:
                return None

            return ReplyCardCore(
                surface=surface,
                zh=zh,
                key_phrases=key_phrases,
            )
        except Exception:
            return None

    async def build_reply_card(
        self,
        *,
        assistant_text: str,
        user_id: uuid.UUID,
        trace_context: dict[str, str] | None = None,
    ) -> ReplyCard | None:
        card_core = await self.generate_reply_card(
            assistant_text=assistant_text,
            trace_context=trace_context,
        )
        if card_core is None:
            return None

        favorite_state = await self.resolve_favorite_state(
            kind="reply_card",
            surface=card_core.surface,
            user_id=user_id,
        )
        return self.attach_reply_card_favorite(card_core, favorite_state)

    async def generate_word_card(
        self,
        *,
        selected_text: str,
        context_text: str | None,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
    ) -> WordCardCore:
        payload = {
            "selected_text": selected_text,
            "context_text": context_text,
        }
        raw = await self._llm_json(
            system_prompt=WORD_CARD_PROMPT.strip(),
            user_message=json.dumps(payload, ensure_ascii=False),
            default={"word_card": {}},
            operation="learning.word_card.generate",
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
        )
        card_data = raw.get("word_card")
        if not isinstance(card_data, dict):
            return self._fallback_word_card_core(selected_text, context_text)

        try:
            pos_groups: list[WordCardPosGroup] = []
            for group in card_data.get("pos_groups", []):
                if not isinstance(group, dict):
                    continue
                pos = (group.get("pos") or "").strip()
                senses: list[WordCardSense] = []
                for sense in group.get("senses", []):
                    if not isinstance(sense, dict):
                        continue
                    zh = (sense.get("zh") or "").strip()
                    if not zh:
                        continue
                    note_raw = sense.get("note")
                    note = note_raw.strip() if isinstance(note_raw, str) and note_raw.strip() else None
                    senses.append(WordCardSense(zh=zh, note=note))
                if pos and senses:
                    pos_groups.append(WordCardPosGroup(pos=pos, senses=senses))

            example_data = card_data.get("example")
            if isinstance(example_data, dict):
                example_surface = (example_data.get("surface") or "").strip()
                example_zh = (example_data.get("zh") or "").strip()
            else:
                example_surface = ""
                example_zh = ""

            if not example_surface or not example_zh:
                return self._fallback_word_card_core(selected_text, context_text)

            ipa_us = (card_data.get("ipa_us") or "").strip() or "/.../"
            return WordCardCore(
                surface=selected_text,
                ipa_us=ipa_us,
                pos_groups=pos_groups,
                example=WordCardExample(surface=example_surface, zh=example_zh),
            )
        except Exception:
            return self._fallback_word_card_core(selected_text, context_text)

    async def generate_feedback_card(
        self,
        *,
        chat_history: list[dict[str, str]],
        user_message_to_improve: str,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
    ) -> FeedbackCardCore | None:
        payload = {
            "chat_history": chat_history,
            "user_message_to_improve": user_message_to_improve,
        }
        raw = await self._llm_json(
            system_prompt=FEEDBACK_CARD_PROMPT.strip(),
            user_message=json.dumps(payload, ensure_ascii=False),
            default={},
            operation="learning.feedback_card.generate",
            trace_context=trace_context,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
        )
        card_data = raw
        if not isinstance(raw, dict):
            logger.warning(
                "Feedback card generation returned unexpected structure: raw_type=%s",
                type(raw).__name__,
            )
            return None

        try:
            key_phrases = self._parse_reply_card_phrases(card_data.get("key_phrases"))[:4]
            surface = (card_data.get("surface") or "").strip() or (user_message_to_improve or "").strip()
            zh = (card_data.get("zh") or "").strip()
            if not surface or not zh:
                logger.warning(
                    "Feedback card generation returned empty required fields: "
                    "surface_provided=%s zh_provided=%s key_phrases_count=%s",
                    bool((card_data.get("surface") or "").strip()),
                    bool((card_data.get("zh") or "").strip()),
                    len(key_phrases),
                )
                return None

            return FeedbackCardCore(
                surface=surface,
                zh=zh,
                key_phrases=key_phrases,
                original=user_message_to_improve,
            )
        except Exception:
            logger.exception("Feedback card core construction failed")
            return None

    @staticmethod
    def attach_default_reply_card_favorite(card_core: ReplyCardCore) -> ReplyCard:
        return ChatLearningService.attach_reply_card_favorite(card_core)

    @staticmethod
    def attach_reply_card_favorite(
        card_core: ReplyCardCore,
        favorite: CardFavoriteState | None = None,
    ) -> ReplyCard:
        favorite_state = favorite or CardFavoriteState(enabled=True, is_favorited=False, saved_item_id=None)
        return ReplyCard(
            surface=card_core.surface,
            zh=card_core.zh,
            key_phrases=card_core.key_phrases,
            favorite=ReplyCardFavorite(
                enabled=favorite_state.enabled,
                is_favorited=favorite_state.is_favorited,
                saved_item_id=favorite_state.saved_item_id,
            ),
        )

    @staticmethod
    def attach_word_card_favorite(
        card_core: WordCardCore,
        favorite: CardFavoriteState | None = None,
    ) -> WordCard:
        favorite_state = favorite or CardFavoriteState(enabled=True, is_favorited=False, saved_item_id=None)
        return WordCard(
            surface=card_core.surface,
            ipa_us=card_core.ipa_us,
            pos_groups=card_core.pos_groups,
            example=card_core.example,
            favorite=favorite_state,
        )

    @staticmethod
    def attach_feedback_card_favorite(
        card_core: FeedbackCardCore,
        favorite: CardFavoriteState | None = None,
    ) -> FeedbackCard:
        favorite_state = favorite or CardFavoriteState(enabled=True, is_favorited=False, saved_item_id=None)
        return FeedbackCard(
            surface=card_core.surface,
            zh=card_core.zh,
            key_phrases=card_core.key_phrases,
            favorite=favorite_state,
            original=card_core.original,
        )

    @staticmethod
    def _parse_reply_card_phrases(items: object) -> list[ReplyCardPhrase]:
        key_phrases: list[ReplyCardPhrase] = []
        if not isinstance(items, list):
            return key_phrases

        for item in items:
            if not isinstance(item, dict):
                continue
            surface = (item.get("surface") or "").strip()
            zh = (item.get("zh") or "").strip()
            ipa_us = (item.get("ipa_us") or "").strip()
            if (
                surface
                and zh
                and ipa_us
                and ipa_us.lower() not in _INVALID_PHRASE_IPA_VALUES
            ):
                key_phrases.append(
                    ReplyCardPhrase(
                        surface=surface,
                        zh=zh,
                        ipa_us=ipa_us,
                    )
                )
        return key_phrases

    @staticmethod
    def _fallback_word_card_core(selected_text: str, context_text: str | None) -> WordCardCore:
        normalized_surface = selected_text if selected_text else "..."
        example_surface = (context_text or "").strip()
        if not example_surface:
            example_surface = f"I used {normalized_surface} in a sentence."
        return WordCardCore(
            surface=normalized_surface,
            ipa_us="/.../",
            pos_groups=[],
            example=WordCardExample(
                surface=example_surface,
                zh="（暂无中文释义）",
            ),
        )
