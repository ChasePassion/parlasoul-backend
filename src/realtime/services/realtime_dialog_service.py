"""Realtime dialog orchestration built on existing LLM services."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncGenerator

from src.db.session import AsyncSessionLocal
from src.repositories.candidate_repository import CandidateRepository
from src.repositories.character_repository import CharacterRepository
from src.repositories.chat_repository import ChatRepository
from src.repositories.turn_repository import TurnRepository
from src.services.character_llm_service import CharacterLLMService
from src.services.chat_generation_service import (
    VOICE_CHANNEL_POLICY,
    ChatGenerationContext,
    build_generation_messages,
)
from src.services.chat_learning_service import ChatLearningService
from src.services.chat_service import (
    _build_user_message_with_images,
    _load_llm_history_messages_from_leaf,
)

__all__ = [
    "VOICE_CHANNEL_POLICY",
    "RealtimeDialogService",
]

logger = logging.getLogger(__name__)


class RealtimeDialogService:
    def __init__(
        self,
        *,
        character_llm_service: CharacterLLMService,
        chat_learning_service: ChatLearningService,
    ) -> None:
        self._character_llm_service = character_llm_service
        self._chat_learning_service = chat_learning_service

    async def translate_assistant_chunk(self, chunk: str) -> str | None:
        if not self._chat_learning_service.contains_english(chunk):
            return None
        translated = await self._chat_learning_service._llm_text(  # noqa: SLF001
            system_prompt="Translate the following English text into natural Chinese. Return only Chinese.",
            user_message=chunk,
            operation="learning.translate.assistant_chunk_to_zh",
        )
        return translated.strip() or None

    async def stream_assistant_reply(
        self,
        *,
        user_id: uuid.UUID,
        chat_id: uuid.UUID,
        character_id: uuid.UUID,
        history_leaf_turn_id: uuid.UUID | None,
        outbound_user_content: str,
    ) -> AsyncGenerator[str, None]:
        async with AsyncSessionLocal() as db:
            prep_started = time.monotonic()
            db.info["current_user_id"] = str(user_id)
            character_repo = CharacterRepository(db)
            chat_repo = ChatRepository(db)
            turn_repo = TurnRepository(db)
            candidate_repo = CandidateRepository(db)

            character = await character_repo.get_by_id(character_id)
            chat = await chat_repo.get_by_id(chat_id)
            if character is None or chat is None:
                return

            resolved_llm_route = await self._character_llm_service.resolve_runtime_route(character)
            history_messages = await _load_llm_history_messages_from_leaf(
                turn_repo=turn_repo,
                candidate_repo=candidate_repo,
                chat_id=chat.id,
                leaf_turn_id=history_leaf_turn_id,
                limit=20,
            )
            generation_context = ChatGenerationContext(
                character_system_prompt=character.system_prompt,
                history_messages=history_messages,
                user_message=outbound_user_content,
                memories={"episodic": [], "semantic": []},
                use_all_semantic=False,
                preferred_expressions=[],
                memory_active=False,
                channel_policy="voice",
            )
            messages = build_generation_messages(
                generation_context,
                user_message_builder=_build_user_message_with_images,
            )
            prep_ms = int((time.monotonic() - prep_started) * 1000)

            stream_started = time.monotonic()
            first_chunk_ms: int | None = None
            chunk_count = 0
            chunk_chars = 0
            last_chunk_at: float | None = None
            max_chunk_gap_ms = 0
            async for chunk in self._character_llm_service.chat_stream_messages(
                messages=messages,
                route=resolved_llm_route,
                operation="chat.reply.realtime",
                route_role="chat",
                user_id=str(user_id),
                chat_id=str(chat_id),
                character_id=str(character_id),
            ):
                now = time.monotonic()
                if first_chunk_ms is None:
                    first_chunk_ms = int((now - stream_started) * 1000)
                if last_chunk_at is not None:
                    max_chunk_gap_ms = max(
                        max_chunk_gap_ms,
                        int((now - last_chunk_at) * 1000),
                    )
                last_chunk_at = now
                chunk_count += 1
                chunk_chars += len(chunk)
                yield chunk
            total_ms = int((time.monotonic() - stream_started) * 1000)
            logger.info(
                "Realtime reply timing: prep_ms=%s first_chunk_ms=%s total_ms=%s chunks=%s "
                "chars=%s max_chunk_gap_ms=%s prompt_msgs=%s history_msgs=%s",
                prep_ms,
                first_chunk_ms,
                total_ms,
                chunk_count,
                chunk_chars,
                max_chunk_gap_ms,
                len(messages),
                len(history_messages),
                extra={
                    "event": "realtime.reply.timing",
                    "chat_id": str(chat_id),
                    "character_id": str(character_id),
                },
            )
