"""Turn service: select candidate, regen assistant, edit user (creates new branch) with SSE."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.api.response import APIError
from src.core.config import settings as app_settings
from src.core.uow import UnitOfWork
from src.db.models import (
    AuthorType,
    Candidate,
    Character,
    ChatOriginType,
    ChatState,
    Turn,
    TurnState,
    User,
)
from src.memory_system import MemoryRecord
from src.memory_system.exceptions import LLMAuthenticationError, LLMCallError
from src.observability import (
    LLMFirstChunkTimeoutError,
    current_trace_context,
    start_workflow_observation,
    update_observation_safe,
)
from src.repositories.candidate_repository import CandidateRepository
from src.repositories.character_repository import CharacterRepository
from src.repositories.chat_repository import ChatRepository
from src.repositories.memory_repository import MemoryRepository
from src.repositories.turn_repository import TurnRepository
from src.repositories.user_settings_repository import UserSettingsRepository
from src.schemas.chat import ChatImageRef
from src.schemas.learning import FeedbackCardPayload, ReplyCardPayload
from src.services.bg_tasks import spawn_bg_task
from src.services.character_access_service import assert_character_chat_writable
from src.services.character_llm_route_service import ResolvedCharacterLLMRoute
from src.services.character_llm_service import CharacterLLMService
from src.services.chat_generation_service import ChatGenerationContext, build_generation_messages
from src.services.chat_learning_service import ChatLearningService
from src.services.chat_service import (
    _build_user_message_with_images,
    _load_llm_history_messages_from_leaf,
    _materialize_chat_image_refs,
    _message_has_image_parts,
    _normalize_requested_chat_images,
)
from src.services.memory_integration import safe_memory_search
from src.services.stream_post_done_tail import (
    PostDoneTailTasks,
    ReplyCardCoreReady,
    start_post_done_tail_tasks,
)
from src.services.subscription_service import SubscriptionService
from src.services.tts_text_segmenter import ConversationTTSSegmenter
from src.services.voice_service import VoiceService

logger = logging.getLogger(__name__)

CONTINUE_PROMPT = (
    "Continue the current interaction in the way based on the recent context, "
    "relationship, mood, topic, environment, and any ongoing storyline. "
    "This may be a casual conversation, a discussion, emotional companionship, "
    "language practice, or something in between. "
    "You may speak, act, react emotionally, shift the mood, reveal something, "
    "introduce a new detail, challenge the user, continue the topic, add a sharper opinion, "
    "ask something, create a small event, or move the scene forward. "
    "If there is an active story or scene, move it forward while leaving space for the user "
    "to step in: a choice, a pause, an unfinished action, a secret to question, "
    "an object to accept or refuse, or a situation they can interrupt. "
    "If the interaction is mainly a conversation or topic discussion, continue it: "
    "add a thought, give a reaction, share a view, notice something, "
    "push the topic deeper, or change direction naturally. "
    "Do not always end with a question. "
    "Do not be generic, overly polite, or assistant-like. "
    "Make the next message feel alive, proactive, and specific to this character."
)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# System prompt construction lives in the shared generation seam
# (src/services/chat_generation_service.py). The orchestration functions below
# (_upsert_candidate_extra, _finalize_*_success/error, TTS worker closure)
# have near-identical counterparts in chat_service.py.
#
# _load_llm_history_messages_from_leaf and _build_user_message_with_images are
# imported from chat_service.py — do NOT duplicate them here.
# ---------------------------------------------------------------------------



async def _is_turn_on_active_branch(
    turn_repo: TurnRepository,
    *,
    chat_id: uuid.UUID,
    active_leaf_turn_id: uuid.UUID | None,
    target_turn_id: uuid.UUID,
) -> bool:
    cur = active_leaf_turn_id
    while cur is not None:
        if cur == target_turn_id:
            return True
        turn = await turn_repo.get_by_id(cur)
        if not turn or turn.chat_id != chat_id:
            break
        cur = turn.parent_turn_id
    return False


async def _recompute_leaf_from_turn(turn_repo: TurnRepository, turn_id: uuid.UUID) -> uuid.UUID:
    cur_turn = await turn_repo.get_by_id(turn_id)
    if not cur_turn:
        return turn_id

    while True:
        if cur_turn.primary_candidate_id is None:
            return cur_turn.id
        child = await turn_repo.get_child_by_parent_candidate(cur_turn.primary_candidate_id)
        if not child:
            return cur_turn.id
        cur_turn = child


def _is_greeting_turn(turn: Turn) -> bool:
    # Greeting turns are proactive assistant messages that start a chat and have no parent edge.
    return (
        turn.author_type == AuthorType.CHARACTER.value
        and bool(turn.is_proactive)
        and turn.parent_turn_id is None
        and turn.parent_candidate_id is None
    )


@dataclass(frozen=True)
class _RegenPrep:
    chat_id: uuid.UUID
    character: Character
    resolved_llm_route: ResolvedCharacterLLMRoute
    assistant_turn_id: uuid.UUID
    assistant_candidate_id: uuid.UUID
    assistant_candidate_no: int
    history_leaf_turn_id: uuid.UUID | None
    parent_user_candidate_id: uuid.UUID
    user_text: str
    chat_images: tuple[ChatImageRef, ...] = ()


@dataclass(frozen=True)
class _ContinuePrep:
    chat_id: uuid.UUID
    character: Character
    resolved_llm_route: ResolvedCharacterLLMRoute
    source_assistant_turn_id: uuid.UUID
    system_turn_id: uuid.UUID
    system_candidate_id: uuid.UUID
    assistant_turn_id: uuid.UUID
    assistant_candidate_id: uuid.UUID
    history_leaf_turn_id: uuid.UUID | None
    last_user_text: str


@dataclass(frozen=True)
class _UserEditPrep:
    chat_id: uuid.UUID
    character: Character
    resolved_llm_route: ResolvedCharacterLLMRoute
    history_leaf_turn_id: uuid.UUID | None
    user_turn_id: uuid.UUID
    user_candidate_id: uuid.UUID
    user_candidate_no: int
    user_text: str
    assistant_turn_id: uuid.UUID
    assistant_turn_no: int
    assistant_candidate_id: uuid.UUID
    chat_images: tuple[ChatImageRef, ...] = ()


class TurnService:
    def __init__(
        self,
        uow: UnitOfWork,
        chat_repo: ChatRepository,
        turn_repo: TurnRepository,
        candidate_repo: CandidateRepository,
        character_repo: CharacterRepository,
        memory_repo: MemoryRepository,
        user_settings_repo: UserSettingsRepository,
        chat_learning_service: ChatLearningService,
        subscription_service: SubscriptionService,
        voice_service: VoiceService | None = None,
        character_llm_service: CharacterLLMService | None = None,
    ):
        self._uow = uow
        self._chat_repo = chat_repo
        self._turn_repo = turn_repo
        self._candidate_repo = candidate_repo
        self._character_repo = character_repo
        self._memory_repo = memory_repo
        self._user_settings_repo = user_settings_repo
        self._chat_learning_service = chat_learning_service
        self._voice_service = voice_service
        self._character_llm_service = character_llm_service or CharacterLLMService()
        self._subscription_service = subscription_service

    def _schedule_turn_tts_warmup(
        self,
        *,
        operation: str,
        task_name: str,
        chat_id: uuid.UUID,
        turn_id: uuid.UUID,
        candidate_id: uuid.UUID,
        character: Character,
    ) -> None:
        if self._voice_service is None:
            return

        provider = (getattr(character, "voice_provider", "") or "").strip()
        if not provider:
            return

        provider_model = (getattr(character, "voice_model", "") or "").strip() or None
        provider_voice_id = (
            getattr(character, "voice_provider_voice_id", "") or ""
        ).strip() or None
        voice_source_type = (getattr(character, "voice_source_type", "") or "").strip() or "system"

        spawn_bg_task(
            self._voice_service.warm_tts_sse_events_from_segments(
                provider=provider,
                provider_voice_id=provider_voice_id,
                provider_model=provider_model,
                voice_source_type=voice_source_type,
            ),
            name=task_name,
            ctx={
                "chat_id": str(chat_id),
                "turn_id": str(turn_id),
                "candidate_id": str(candidate_id),
                "character_id": str(character.id),
                "provider": provider,
                "provider_voice_id": provider_voice_id,
            },
        )
        logger.info(
            "Turn TTS warm-up scheduled.",
            extra={
                "log_module": "voice",
                "event": "turn.tts.warm_scheduled",
                "operation": operation,
                "provider": provider,
                "model": provider_model,
                "provider_voice_id": provider_voice_id,
                "audio_format": "pcm",
                "chat_id": str(chat_id),
                "turn_id": str(turn_id),
                "candidate_id": str(candidate_id),
                "character_id": str(character.id),
            },
        )

    async def generate_feedback_card(
        self,
        *,
        current_user: User,
        turn_id: uuid.UUID,
    ) -> FeedbackCardPayload:
        turn = await self._turn_repo.get_by_id(turn_id)
        if not turn:
            raise APIError(
                status_code=404,
                code="turn_not_found",
                message=f"resource not found: turn does not exist; turn_id={turn_id}",
            )

        chat = await self._chat_repo.get_by_id(turn.chat_id)
        if not chat:
            raise APIError(
                status_code=404,
                code="chat_not_found",
                message=f"resource not found: chat does not exist; chat_id={turn.chat_id}",
            )

        if chat.user_id != current_user.id:
            raise APIError(
                status_code=403,
                code="chat_forbidden",
                message=f"authorization failed: chat is not accessible; chat_id={chat.id}",
            )

        if turn.author_type != AuthorType.USER.value:
            raise APIError(
                status_code=409,
                code="feedback_target_invalid",
                message=(
                    "resource conflict: feedback is only supported for user turns; "
                    f"turn_id={turn.id}; author_type={turn.author_type}"
                ),
            )

        if not await _is_turn_on_active_branch(
            self._turn_repo,
            chat_id=chat.id,
            active_leaf_turn_id=chat.active_leaf_turn_id,
            target_turn_id=turn.id,
        ):
            raise APIError(
                status_code=409,
                code="feedback_target_not_on_active_branch",
                message=(
                    "resource conflict: feedback target must be on the active branch; "
                    f"turn_id={turn.id}; chat_id={chat.id}"
                ),
            )

        if not turn.primary_candidate_id:
            raise APIError(
                status_code=409,
                code="feedback_target_missing_candidate",
                message=(
                    f"resource conflict: user turn has no primary candidate; turn_id={turn.id}"
                ),
            )

        candidate = await self._candidate_repo.get_by_id(turn.primary_candidate_id)
        candidate_content = (candidate.content if candidate else "").strip()
        if not candidate_content:
            raise APIError(
                status_code=409,
                code="feedback_target_missing_content",
                message=(
                    f"resource conflict: user turn has no content for feedback; turn_id={turn.id}"
                ),
            )

        history_messages = await _load_llm_history_messages_from_leaf(
            turn_repo=self._turn_repo,
            candidate_repo=self._candidate_repo,
            chat_id=chat.id,
            leaf_turn_id=turn.id,
            limit=app_settings.CHAT_HISTORY_LIMIT,
        )
        if history_messages and history_messages[-1]["role"] == "user":
            chat_history = history_messages[:-1]
        else:
            chat_history = history_messages
        trace_context = None
        extra = getattr(candidate, "extra", None)
        if isinstance(extra, dict):
            raw_context = extra.get("observability_context")
            if isinstance(raw_context, dict):
                trace_id = raw_context.get("trace_id")
                parent_span_id = raw_context.get("parent_span_id")
                if isinstance(trace_id, str) and isinstance(parent_span_id, str):
                    trace_context = {
                        "trace_id": trace_id,
                        "parent_span_id": parent_span_id,
                    }
        workflow_context = start_workflow_observation(
            name="chat.turn.feedback_card",
            input_payload={
                "chat_id": str(chat.id),
                "turn_id": str(turn.id),
                "candidate_id": str(turn.primary_candidate_id),
                "content_length": len(candidate_content),
            },
            trace_context=trace_context,
            user_id=str(current_user.id),
            session_id=f"chat:{chat.id}",
            tags=["chat", "turn", "feedback_card"],
            metadata={
                "workflow": "learning.feedback_card.request",
                "chat_id": str(chat.id),
                "turn_id": str(turn.id),
                "candidate_id": str(turn.primary_candidate_id),
            },
        )
        workflow_observation = workflow_context.__enter__()
        try:
            feedback_core = await self._chat_learning_service.generate_feedback_card(
                chat_history=chat_history,
                user_message_to_improve=candidate_content,
                user_id=str(current_user.id),
                chat_id=str(chat.id),
                turn_id=str(turn.id),
                candidate_id=str(turn.primary_candidate_id),
                character_id=(
                    str(chat.character_id)
                    if getattr(chat, "character_id", None) is not None
                    else None
                ),
            )
            if feedback_core is None:
                raise APIError(
                    status_code=500,
                    code="feedback_card_generation_failed",
                    message="feedback card generation returned invalid payload",
                )
            favorite_state = await self._chat_learning_service.resolve_favorite_state(
                kind="feedback_card",
                surface=getattr(feedback_core, "original", None) or feedback_core.surface,
                user_id=current_user.id,
            )
            feedback_card = self._chat_learning_service.attach_feedback_card_favorite(
                feedback_core,
                favorite_state,
            )
            update_observation_safe(
                workflow_observation,
                output={
                    "status": "completed",
                    "surface": feedback_card.surface,
                    "favorite_enabled": feedback_card.favorite.enabled,
                },
            )
            return FeedbackCardPayload(feedback_card=feedback_card)
        except Exception as exc:
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=str(exc),
                output={"status": "error"},
            )
            raise
        finally:
            workflow_context.__exit__(*sys.exc_info())

    async def generate_reply_card_for_candidate(
        self,
        *,
        current_user: User,
        candidate_id: uuid.UUID,
    ) -> ReplyCardPayload:
        row = await self._candidate_repo.get_assistant_candidate_with_chat(
            candidate_id=candidate_id,
            user_id=current_user.id,
        )
        if row is None:
            raise APIError(
                status_code=404,
                code="candidate_not_found",
                message=f"resource not found: candidate does not exist; candidate_id={candidate_id}",
            )

        candidate, turn, chat = row
        assistant_text = (candidate.content or "").strip()
        if not assistant_text:
            raise APIError(
                status_code=409,
                code="reply_card_target_missing_content",
                message=(
                    "resource conflict: assistant candidate has no content for reply card; "
                    f"candidate_id={candidate_id}"
                ),
            )
        trace_context = None
        extra = getattr(candidate, "extra", None)
        if isinstance(extra, dict):
            raw_context = extra.get("observability_context")
            if isinstance(raw_context, dict):
                trace_id = raw_context.get("trace_id")
                parent_span_id = raw_context.get("parent_span_id")
                if isinstance(trace_id, str) and isinstance(parent_span_id, str):
                    trace_context = {
                        "trace_id": trace_id,
                        "parent_span_id": parent_span_id,
                    }

        workflow_context = start_workflow_observation(
            name="chat.turn.reply_card",
            input_payload={
                "chat_id": str(chat.id),
                "turn_id": str(turn.id),
                "candidate_id": str(candidate.id),
                "content_length": len(assistant_text),
            },
            trace_context=trace_context,
            user_id=str(current_user.id),
            session_id=f"chat:{chat.id}",
            tags=["chat", "turn", "reply_card"],
            metadata={
                "workflow": "learning.reply_card.request",
                "chat_id": str(chat.id),
                "turn_id": str(turn.id),
                "candidate_id": str(candidate.id),
            },
        )
        workflow_observation = workflow_context.__enter__()
        try:
            reply_card = await self._chat_learning_service.build_reply_card(
                assistant_text=assistant_text,
                user_id=current_user.id,
            )
            if reply_card is None:
                raise APIError(
                    status_code=500,
                    code="reply_card_generation_failed",
                    message="reply card generation returned invalid payload",
                )

            await self._upsert_candidate_extra(
                candidate_id=candidate.id,
                payload={"reply_card": reply_card.model_dump()},
                commit=True,
            )
            update_observation_safe(
                workflow_observation,
                output={
                    "status": "completed",
                    "surface": reply_card.surface,
                    "favorite_enabled": reply_card.favorite.enabled,
                },
            )
            return ReplyCardPayload(reply_card=reply_card)
        except Exception as exc:
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=str(exc),
                output={"status": "error"},
            )
            raise
        finally:
            workflow_context.__exit__(*sys.exc_info())

    async def _render_post_done_item(
        self,
        *,
        item: dict[str, Any] | ReplyCardCoreReady,
        current_user: User,
        assistant_candidate_id: uuid.UUID,
        persistence_log_message: str,
    ) -> list[str]:
        if not isinstance(item, ReplyCardCoreReady):
            return [_sse_event(item)]

        try:
            favorite_state = await self._chat_learning_service.resolve_favorite_state(
                kind="reply_card",
                surface=item.card_core.surface,
                user_id=current_user.id,
            )
            reply_card = self._chat_learning_service.attach_reply_card_favorite(
                item.card_core,
                favorite_state,
            )
            await self._upsert_candidate_extra(
                candidate_id=assistant_candidate_id,
                payload={"reply_card": reply_card.model_dump()},
                commit=True,
            )
        except Exception:
            logger.exception(persistence_log_message)
            return [
                _sse_event(
                    {
                        "type": "reply_card_error",
                        "assistant_candidate_id": str(assistant_candidate_id),
                        "code": "reply_card_persist_failed",
                        "message": "internal error: reply card persistence failed",
                    }
                )
            ]

        return [
            _sse_event(
                {
                    "type": "reply_card",
                    "assistant_candidate_id": str(assistant_candidate_id),
                    "reply_card": reply_card.model_dump(),
                }
            )
        ]

    async def select_candidate(
        self,
        current_user: User,
        turn_id: uuid.UUID,
        candidate_no: int,
    ) -> dict[str, Any]:
        async with self._uow.transactional():
            turn = await self._turn_repo.get_by_id(turn_id)
            if not turn:
                raise APIError(
                    status_code=404,
                    code="turn_not_found",
                    message=f"resource not found: turn does not exist; turn_id={turn_id}",
                )

            chat = await self._chat_repo.get_by_id(turn.chat_id)
            if not chat:
                raise APIError(
                    status_code=404,
                    code="chat_not_found",
                    message=f"resource not found: chat does not exist; chat_id={turn.chat_id}",
                )

            if chat.user_id != current_user.id:
                raise APIError(
                    status_code=403,
                    code="chat_forbidden",
                    message=f"authorization failed: chat is not accessible; chat_id={chat.id}",
                )

            if _is_greeting_turn(turn):
                raise APIError(
                    status_code=409,
                    code="greeting_turn_locked",
                    message=(
                        "resource conflict: greeting turn does not support candidate switching; "
                        f"turn_id={turn.id}"
                    ),
                )

            cand = await self._candidate_repo.get_by_turn_and_no(
                turn_id=turn.id,
                candidate_no=candidate_no,
            )
            if not cand:
                raise APIError(
                    status_code=404,
                    code="candidate_not_found",
                    message=(
                        "resource not found: candidate does not exist for turn; "
                        f"turn_id={turn.id}; candidate_no={candidate_no}"
                    ),
                )

            turn.primary_candidate_id = cand.id

            leaf_id = await _recompute_leaf_from_turn(self._turn_repo, turn.id)
            chat.active_leaf_turn_id = leaf_id
            chat.last_turn_id = leaf_id

            # candidate_count for UI k/n
            candidate_count = await self._candidate_repo.get_max_candidate_no(turn_id=turn.id)
            candidate_count = int(candidate_count or 1)

        return {
            "chat_id": str(chat.id),
            "turn_id": str(turn.id),
            "primary_candidate_id": str(cand.id),
            "primary_candidate_no": int(cand.candidate_no),
            "candidate_count": candidate_count,
            "active_leaf_turn_id": str(leaf_id),
        }

    async def prepare_regen(
        self,
        current_user: User,
        assistant_turn_id: uuid.UUID,
    ) -> _RegenPrep:
        """Validate and create the new assistant candidate placeholder before SSE starts."""
        try:
            assistant_turn = await self._turn_repo.get_for_update(assistant_turn_id)
            if not assistant_turn:
                raise APIError(
                    status_code=404,
                    code="turn_not_found",
                    message=f"resource not found: turn does not exist; turn_id={assistant_turn_id}",
                )

            if assistant_turn.author_type != AuthorType.CHARACTER.value:
                raise APIError(
                    status_code=409,
                    code="turn_author_conflict",
                    message=(
                        "resource conflict: regen is only supported for assistant turns; "
                        f"turn_id={assistant_turn.id}; author_type={assistant_turn.author_type}"
                    ),
                )

            if _is_greeting_turn(assistant_turn):
                raise APIError(
                    status_code=409,
                    code="greeting_turn_locked",
                    message=(
                        "resource conflict: greeting turn does not support regen; "
                        f"turn_id={assistant_turn.id}"
                    ),
                )

            chat = await self._chat_repo.get_for_update(assistant_turn.chat_id)
            if not chat:
                raise APIError(
                    status_code=404,
                    code="chat_not_found",
                    message=f"resource not found: chat does not exist; chat_id={assistant_turn.chat_id}",
                )

            if chat.user_id != current_user.id:
                raise APIError(
                    status_code=403,
                    code="chat_forbidden",
                    message=f"authorization failed: chat is not accessible; chat_id={chat.id}",
                )

            if chat.state != ChatState.ACTIVE.value:
                raise APIError(
                    status_code=409,
                    code="chat_state_conflict",
                    message=f"resource conflict: chat is not active; chat_id={chat.id}; state={chat.state}",
                )

            character = await self._character_repo.get_by_id(chat.character_id)
            if not character:
                raise APIError(
                    status_code=404,
                    code="character_not_found",
                    message=f"resource not found: character does not exist; character_id={chat.character_id}",
                )
            assert_character_chat_writable(character, user_id=current_user.id)
            resolved_llm_route = await self._character_llm_service.resolve_runtime_route(character)

            max_no = await self._candidate_repo.get_max_candidate_no(turn_id=assistant_turn.id)
            if max_no >= 10:
                raise APIError(
                    status_code=409,
                    code="turn_candidate_limit_reached",
                    message=f"resource conflict: candidate limit reached; turn_id={assistant_turn.id}; limit=10",
                )

            new_no = max_no + 1
            now = _now_utc()

            new_cand = Candidate(
                turn_id=assistant_turn.id,
                candidate_no=new_no,
                content="",
                is_final=False,
                extra={"source": "regen_placeholder"},
            )
            self._candidate_repo.add(new_cand)
            await self._uow.flush()

            assistant_turn.primary_candidate_id = new_cand.id
            assistant_turn.state = TurnState.OK.value

            # A new candidate has no downstream child yet, so the active leaf truncates here.
            chat.active_leaf_turn_id = assistant_turn.id
            chat.last_turn_id = assistant_turn.id
            chat.last_turn_at = now

            # Build regen context from parent user candidate.
            if assistant_turn.parent_turn_id is None or assistant_turn.parent_candidate_id is None:
                raise APIError(
                    status_code=409,
                    code="turn_state_conflict",
                    message=(
                        "resource conflict: assistant turn has no parent user candidate; "
                        f"turn_id={assistant_turn.id}"
                    ),
                )

            parent_user_turn = await self._turn_repo.get_by_id(assistant_turn.parent_turn_id)
            if not parent_user_turn or parent_user_turn.chat_id != chat.id:
                raise APIError(
                    status_code=409,
                    code="turn_state_conflict",
                    message=(
                        "resource conflict: assistant turn parent is invalid; "
                        f"turn_id={assistant_turn.id}; parent_turn_id={assistant_turn.parent_turn_id}"
                    ),
                )

            parent_user_cand = await self._candidate_repo.get_by_id(
                assistant_turn.parent_candidate_id
            )
            if not parent_user_cand:
                raise APIError(
                    status_code=409,
                    code="turn_state_conflict",
                    message=(
                        "resource conflict: assistant turn parent candidate is missing; "
                        f"turn_id={assistant_turn.id}; parent_candidate_id={assistant_turn.parent_candidate_id}"
                    ),
                )

            user_text = (parent_user_cand.content or "").strip()
            if not user_text:
                raise APIError(
                    status_code=409,
                    code="turn_state_conflict",
                    message=(
                        "resource conflict: parent user content is empty; "
                        f"turn_id={assistant_turn.id}; parent_candidate_id={assistant_turn.parent_candidate_id}"
                    ),
                )

            chat_image_refs = _materialize_chat_image_refs(
                (parent_user_cand.extra or {}).get("chat_images")
            )
            chat_images = tuple(chat_image_refs) if chat_image_refs else ()

            history_leaf_turn_id = parent_user_turn.parent_turn_id
            parent_user_candidate_id = parent_user_cand.id

            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise

        return _RegenPrep(
            chat_id=chat.id,
            character=character,
            resolved_llm_route=resolved_llm_route,
            assistant_turn_id=assistant_turn.id,
            assistant_candidate_id=new_cand.id,
            assistant_candidate_no=new_no,
            history_leaf_turn_id=history_leaf_turn_id,
            parent_user_candidate_id=parent_user_candidate_id,
            user_text=user_text,
            chat_images=chat_images,
        )

    async def stream_regen_with_prep(
        self,
        prep: _RegenPrep,
        current_user: User,
    ) -> AsyncGenerator[str, None]:
        yield _sse_event(
            {
                "type": "meta",
                "assistant_turn": {
                    "id": str(prep.assistant_turn_id),
                    "candidate_id": str(prep.assistant_candidate_id),
                    "candidate_no": prep.assistant_candidate_no,
                },
            }
        )

        accumulated = ""
        outbound_user_content = prep.user_text
        tts_enabled = False
        tts_provider: str | None = None
        tts_provider_voice_id: str | None = None
        tts_provider_model: str | None = None
        tts_voice_source_type: str | None = None
        tts_segmenter: ConversationTTSSegmenter | None = None
        tts_worker_done = False
        tts_error_emitted = False
        tts_segments_queue: asyncio.Queue[str | None] | None = None
        tts_events_queue: asyncio.Queue[dict[str, Any]] | None = None
        tts_worker_task: asyncio.Task[None] | None = None
        post_done_tasks: PostDoneTailTasks | None = None
        workflow_context = start_workflow_observation(
            name="chat.turn.regen",
            input_payload={
                "chat_id": str(prep.chat_id),
                "assistant_turn_id": str(prep.assistant_turn_id),
                "assistant_candidate_id": str(prep.assistant_candidate_id),
                "parent_user_candidate_id": str(prep.parent_user_candidate_id),
                "content_length": len(prep.user_text),
            },
            user_id=str(current_user.id),
            session_id=f"chat:{prep.chat_id}",
            tags=["chat", "turn", "regen"],
            metadata={
                "workflow": "chat.reply.regen",
                "chat_id": str(prep.chat_id),
                "character_id": str(prep.character.id),
                "assistant_candidate_id": str(prep.assistant_candidate_id),
            },
            manual=True,
        )
        workflow_observation = workflow_context.__enter__()
        try:
            workflow_trace_context = current_trace_context(workflow_observation)
            if workflow_trace_context is not None:
                await self._upsert_candidate_extra(
                    candidate_id=prep.assistant_candidate_id,
                    payload={"observability_context": workflow_trace_context},
                    commit=True,
                )
            user_settings = await self._user_settings_repo.get_or_create(
                current_user.id,
                default_font_size=app_settings.USER_SETTINGS_MESSAGE_FONT_SIZE_DEFAULT,
                default_display_mode=app_settings.USER_SETTINGS_DISPLAY_MODE_DEFAULT,
                default_memory_enabled=True,
                default_reply_card_enabled=app_settings.USER_SETTINGS_REPLY_CARD_ENABLED_DEFAULT,
                default_mixed_input_auto_translate_enabled=(
                    app_settings.USER_SETTINGS_MIXED_INPUT_AUTO_TRANSLATE_ENABLED_DEFAULT
                ),
                default_auto_read_aloud_enabled=(
                    app_settings.USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT
                ),
                default_preferred_expression_bias_enabled=(
                    app_settings.USER_SETTINGS_PREFERRED_EXPRESSION_BIAS_ENABLED_DEFAULT
                ),
                default_proactive_enabled=app_settings.USER_SETTINGS_PROACTIVE_ENABLED_DEFAULT,
                default_timezone=app_settings.USER_SETTINGS_TIMEZONE_DEFAULT,
            )
            memory_active = await self._subscription_service.has_feature(
                current_user, "memory_feature"
            ) and bool(user_settings.memory_enabled)
            tts_enabled = bool(self._voice_service and user_settings.auto_read_aloud_enabled)
            if tts_enabled and prep.character.voice_provider_voice_id.strip():
                tts_provider = prep.character.voice_provider
                tts_provider_voice_id = prep.character.voice_provider_voice_id
                tts_provider_model = prep.character.voice_model
                tts_voice_source_type = prep.character.voice_source_type
            if tts_enabled:
                self._schedule_turn_tts_warmup(
                    operation="chat.reply.regen",
                    task_name="turn.regen.tts_warmup",
                    chat_id=prep.chat_id,
                    turn_id=prep.assistant_turn_id,
                    candidate_id=prep.assistant_candidate_id,
                    character=prep.character,
                )

            def _consume_tts_event(event: dict[str, Any]) -> str | None:
                nonlocal tts_worker_done
                nonlocal tts_error_emitted
                event_type = event.get("type")
                if event_type == "tts_worker_done":
                    tts_worker_done = True
                    return None
                if event_type == "tts_error":
                    tts_error_emitted = True
                return _sse_event(event)

            def _drain_tts_frames() -> list[str]:
                frames: list[str] = []
                if not tts_events_queue:
                    return frames
                while True:
                    try:
                        event = tts_events_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    frame = _consume_tts_event(event)
                    if frame:
                        frames.append(frame)
                return frames

            async def _wait_tts_frame(timeout_s: float) -> str | None:
                if not tts_events_queue:
                    return None
                try:
                    event = await asyncio.wait_for(tts_events_queue.get(), timeout=timeout_s)
                except TimeoutError:
                    return None
                return _consume_tts_event(event)

            async def _drain_post_done_frames() -> list[str]:
                if post_done_tasks is None:
                    return []

                frames: list[str] = []
                for item in post_done_tasks.drain_nowait():
                    frames.extend(
                        await self._render_post_done_item(
                            item=item,
                            current_user=current_user,
                            assistant_candidate_id=prep.assistant_candidate_id,
                            persistence_log_message="Regen reply card persistence failed",
                        )
                    )
                return frames

            if tts_enabled:
                tts_segments_queue = asyncio.Queue()
                tts_events_queue = asyncio.Queue()
                tts_segmenter = ConversationTTSSegmenter()

                async def _tts_worker() -> None:
                    assert self._voice_service is not None
                    assert tts_segments_queue is not None
                    assert tts_events_queue is not None
                    try:
                        async for event in self._voice_service.stream_tts_sse_events_from_segments(
                            segments_queue=tts_segments_queue,
                            assistant_candidate_id=str(prep.assistant_candidate_id),
                            emit_done=False,
                            provider=tts_provider or prep.character.voice_provider,
                            provider_voice_id=tts_provider_voice_id,
                            provider_model=tts_provider_model,
                            voice_source_type=tts_voice_source_type
                            or prep.character.voice_source_type,
                        ):
                            await tts_events_queue.put(event)
                    except APIError as exc:
                        await tts_events_queue.put(
                            {
                                "type": "tts_error",
                                "code": exc.code,
                                "message": exc.message,
                            }
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.exception("Regen TTS worker failed: %s", exc)
                        await tts_events_queue.put(
                            {
                                "type": "tts_error",
                                "code": "tts_upstream_error",
                                "message": (
                                    "upstream error: tts synthesis failed during regen stream"
                                ),
                            }
                        )
                    finally:
                        await tts_events_queue.put({"type": "tts_worker_done"})

                tts_worker_task = asyncio.create_task(
                    _tts_worker(),
                    name="turn.regen.tts_worker",
                )

            parent_user_cand = await self._candidate_repo.get_by_id(prep.parent_user_candidate_id)
            if parent_user_cand:
                existing_transform = (parent_user_cand.extra or {}).get("input_transform")
                if isinstance(existing_transform, dict):
                    transformed_content = (
                        existing_transform.get("transformed_content") or ""
                    ).strip()
                    if transformed_content:
                        outbound_user_content = transformed_content
            if (
                outbound_user_content == prep.user_text
                and user_settings.mixed_input_auto_translate_enabled
                and self._chat_learning_service.contains_chinese(prep.user_text)
            ):
                translated = prep.user_text
                try:
                    translated = await self._chat_learning_service.translate_mixed_to_english(
                        prep.user_text,
                        user_id=str(current_user.id),
                        chat_id=str(prep.chat_id),
                        turn_id=str(prep.assistant_turn_id),
                        character_id=str(prep.character.id),
                    )
                except Exception:
                    logger.exception(
                        "Regen mixed-input translation failed; fallback to original content"
                    )
                    translated = prep.user_text

                outbound_user_content = translated or prep.user_text
                transform_payload = {
                    "applied": bool(
                        outbound_user_content.strip() and outbound_user_content != prep.user_text
                    ),
                    "transformed_content": outbound_user_content,
                }
                await self._upsert_candidate_extra(
                    candidate_id=prep.parent_user_candidate_id,
                    payload={"input_transform": transform_payload},
                )

            history_messages = await _load_llm_history_messages_from_leaf(
                turn_repo=self._turn_repo,
                candidate_repo=self._candidate_repo,
                chat_id=prep.chat_id,
                leaf_turn_id=prep.history_leaf_turn_id,
                limit=app_settings.CHAT_HISTORY_LIMIT,
            )

            relevant_memories: dict[str, list[MemoryRecord]] = {"episodic": [], "semantic": []}
            if memory_active:
                relevant_memories = await safe_memory_search(
                    self._memory_repo,
                    query=outbound_user_content,
                    user_id=str(current_user.id),
                    character_id=str(prep.character.id),
                    chat_id=str(prep.chat_id),
                    timeout_s=self._memory_repo.allowed_memory_search_timeout_s,
                )
            preferred_expressions = []
            if user_settings.preferred_expression_bias_enabled:
                preferred_expressions = (
                    await self._chat_learning_service.load_preferred_expressions(
                        user_id=current_user.id,
                    )
                )

            generation_context = ChatGenerationContext(
                character_system_prompt=prep.character.system_prompt,
                history_messages=history_messages,
                user_message=outbound_user_content,
                memories=relevant_memories,
                use_all_semantic=(
                    self._memory_repo.use_all_semantic if memory_active else False
                ),
                preferred_expressions=preferred_expressions,
                chat_images=prep.chat_images,
                memory_active=memory_active,
                channel_policy="chat",
            )
            messages = build_generation_messages(
                generation_context,
                user_message_builder=_build_user_message_with_images,
            )
            regen_has_multimodal = bool(prep.chat_images) or any(
                _message_has_image_parts(m) for m in history_messages
            )
            regen_active_route = (
                self._character_llm_service.resolve_vision_route()
                if regen_has_multimodal
                else prep.resolved_llm_route
            )
            async for chunk in self._character_llm_service.chat_stream_messages(
                messages=messages,
                route=regen_active_route,
                operation="chat.reply.regen",
                route_role="chat",
                trace_context=workflow_trace_context,
                user_id=str(current_user.id),
                chat_id=str(prep.chat_id),
                turn_id=str(prep.assistant_turn_id),
                candidate_id=str(prep.assistant_candidate_id),
                character_id=str(prep.character.id),
            ):
                accumulated += chunk
                yield _sse_event({"type": "chunk", "content": chunk})
                if tts_enabled and tts_segments_queue is not None and tts_segmenter is not None:
                    for segment in tts_segmenter.feed(chunk):
                        await tts_segments_queue.put(segment)
                for frame in _drain_tts_frames():
                    yield frame

            await self._finalize_regen_success(
                prep=prep,
                full_content=accumulated,
                model_type=prep.resolved_llm_route.model_type_label,
            )
            if tts_enabled and tts_segments_queue is not None and tts_segmenter is not None:
                for tail_segment in tts_segmenter.flush():
                    await tts_segments_queue.put(tail_segment)
                await tts_segments_queue.put(None)
                for frame in _drain_tts_frames():
                    yield frame

            yield _sse_event(
                {
                    "type": "done",
                    "full_content": accumulated,
                    "assistant_turn_id": str(prep.assistant_turn_id),
                    "assistant_candidate_id": str(prep.assistant_candidate_id),
                }
            )

            chat_history_for_suggestions = (
                history_messages + [_build_user_message_with_images(outbound_user_content, list(prep.chat_images))]
            )[-app_settings.CHAT_HISTORY_LIMIT :]
            should_generate_card = (
                user_settings.reply_card_enabled or user_settings.display_mode == "detailed"
            )
            if should_generate_card:
                yield _sse_event(
                    {
                        "type": "reply_card_started",
                        "assistant_candidate_id": str(prep.assistant_candidate_id),
                    }
                )

            post_done_tasks = start_post_done_tail_tasks(
                assistant_candidate_id=str(prep.assistant_candidate_id),
                suggestions_coro=self._chat_learning_service.generate_reply_suggestions(
                    chat_history=chat_history_for_suggestions,
                    assistant_latest_message=accumulated,
                    preferred_expressions=preferred_expressions,
                    route=prep.resolved_llm_route,
                    trace_context=workflow_trace_context,
                    user_id=str(current_user.id),
                    chat_id=str(prep.chat_id),
                    turn_id=str(prep.assistant_turn_id),
                    candidate_id=str(prep.assistant_candidate_id),
                    character_id=str(prep.character.id),
                ),
                logger=logger,
                suggestions_error_log_message="Regen reply suggestions generation failed",
                reply_card_core_coro=(
                    self._chat_learning_service.generate_reply_card(
                        assistant_text=accumulated,
                        trace_context=workflow_trace_context,
                        user_id=str(current_user.id),
                        chat_id=str(prep.chat_id),
                        turn_id=str(prep.assistant_turn_id),
                        candidate_id=str(prep.assistant_candidate_id),
                        character_id=str(prep.character.id),
                    )
                    if should_generate_card
                    else None
                ),
                reply_card_error_log_message="Regen reply card generation failed",
            )

            if tts_enabled and tts_worker_task is not None:
                deadline = time.monotonic() + 60.0
                while not tts_worker_done:
                    for frame in _drain_tts_frames():
                        yield frame
                    for frame in await _drain_post_done_frames():
                        yield frame

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        tts_error_emitted = True
                        yield _sse_event(
                            {
                                "type": "tts_error",
                                "code": "external_timeout",
                                "message": "upstream timeout: tts synthesis timed out",
                            }
                        )
                        break

                    if post_done_tasks is not None and post_done_tasks.pending:
                        next_item = await post_done_tasks.wait_for_item(min(0.05, remaining))
                        if next_item is not None:
                            for frame in await self._render_post_done_item(
                                item=next_item,
                                current_user=current_user,
                                assistant_candidate_id=prep.assistant_candidate_id,
                                persistence_log_message="Regen reply card persistence failed",
                            ):
                                yield frame
                            continue

                    next_frame = await _wait_tts_frame(min(0.05, remaining))
                    if next_frame:
                        yield next_frame
                    elif tts_worker_task.done():
                        for frame in _drain_tts_frames():
                            yield frame
                        if not tts_worker_done:
                            tts_worker_done = True

                if tts_worker_task.done():
                    try:
                        await tts_worker_task
                    except Exception:
                        if not tts_error_emitted:
                            tts_error_emitted = True
                            yield _sse_event(
                                {
                                    "type": "tts_error",
                                    "code": "tts_upstream_error",
                                    "message": "upstream error: tts synthesis failed",
                                }
                            )

                if tts_worker_done and not tts_error_emitted:
                    yield _sse_event(
                        {
                            "type": "tts_audio_done",
                            "assistant_candidate_id": str(prep.assistant_candidate_id),
                        }
                    )

            if post_done_tasks is not None:
                while post_done_tasks.pending:
                    for frame in await _drain_post_done_frames():
                        yield frame
                    if not post_done_tasks.pending:
                        break
                    next_item = await post_done_tasks.wait_for_item(0.05)
                    if next_item is None:
                        continue
                    for frame in await self._render_post_done_item(
                        item=next_item,
                        current_user=current_user,
                        assistant_candidate_id=prep.assistant_candidate_id,
                        persistence_log_message="Regen reply card persistence failed",
                    ):
                        yield frame

            if memory_active:
                spawn_bg_task(
                    self._memory_repo.manage(
                        user_text=outbound_user_content,
                        assistant_text=accumulated,
                        user_id=str(current_user.id),
                        character_id=str(prep.character.id),
                        chat_id=str(prep.chat_id),
                    ),
                    name="memory.manage_async",
                    ctx={
                        "user_id": str(current_user.id),
                        "character_id": str(prep.character.id),
                        "chat_id": str(prep.chat_id),
                    },
                )
                episodic_for_prompt = relevant_memories.get("episodic", [])[:5]
                spawn_bg_task(
                    self._memory_repo.judge_and_assign_narrative_group(
                        episodic_memories=episodic_for_prompt,
                        last_user=outbound_user_content,
                        last_assistant=accumulated,
                        user_id=str(current_user.id),
                        character_id=str(prep.character.id),
                        chat_id=str(prep.chat_id),
                    ),
                    name="memory.judge_and_assign_narrative_group_async",
                    ctx={
                        "user_id": str(current_user.id),
                        "character_id": str(prep.character.id),
                        "chat_id": str(prep.chat_id),
                    },
                )

            update_observation_safe(
                workflow_observation,
                output={
                    "status": "completed",
                    "assistant_candidate_id": str(prep.assistant_candidate_id),
                    "content_length": len(accumulated),
                    "memory_active": memory_active,
                    "tts_enabled": tts_enabled,
                },
            )

        except LLMFirstChunkTimeoutError as e:
            message = (
                "upstream timeout: llm stream did not produce the first chunk in time; "
                f"model={e.model}; timeout_s={e.timeout_s:.1f}"
            )
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "llm_stream_first_chunk_timeout"},
            )
            await self._rollback_after_stream_exception(reason="llm_stream_first_chunk_timeout")
            await self._finalize_regen_error(
                prep=prep,
                code="llm_stream_first_chunk_timeout",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event(
                {
                    "type": "error",
                    "code": "llm_stream_first_chunk_timeout",
                    "message": message,
                }
            )
        except LLMAuthenticationError as e:
            message = e.public_message()
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "llm_service_error"},
            )
            await self._rollback_after_stream_exception(reason="llm_service_error")
            await self._finalize_regen_error(
                prep=prep,
                code="llm_service_error",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "llm_service_error", "message": message})
        except LLMCallError as e:
            message = (
                "upstream error: llm request failed after retries; "
                f"model={e.model}; attempts={e.attempts}"
            )
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "llm_service_error"},
            )
            await self._rollback_after_stream_exception(reason="llm_service_error")
            await self._finalize_regen_error(
                prep=prep,
                code="llm_service_error",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "llm_service_error", "message": message})
        except Exception as e:
            logger.exception("Regen stream error for user %s: %s", current_user.id, e)
            message = "regen stream failed: unexpected streaming error"
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "regen_stream_failed"},
            )
            await self._rollback_after_stream_exception(reason="regen_stream_failed")
            await self._finalize_regen_error(
                prep=prep,
                code="regen_stream_failed",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "regen_stream_failed", "message": message})
        finally:
            if post_done_tasks is not None:
                await post_done_tasks.aclose()
            if tts_worker_task and not tts_worker_task.done():
                tts_worker_task.cancel()
            workflow_context.__exit__(*sys.exc_info())

    async def _finalize_regen_success(
        self, prep: _RegenPrep, full_content: str, model_type: str
    ) -> None:
        now = _now_utc()
        try:
            cand = await self._candidate_repo.get_by_id(prep.assistant_candidate_id)
            turn = await self._turn_repo.get_by_id(prep.assistant_turn_id)
            chat = await self._chat_repo.get_by_id(prep.chat_id)
            character = await self._character_repo.get_by_id(prep.character.id)
            if not cand:
                raise RuntimeError(
                    f"regen finalize failed: candidate not found; candidate_id={prep.assistant_candidate_id}"
                )
            if not turn:
                raise RuntimeError(
                    f"regen finalize failed: turn not found; turn_id={prep.assistant_turn_id}"
                )
            if not chat:
                raise RuntimeError(f"regen finalize failed: chat not found; chat_id={prep.chat_id}")
            if not character:
                raise RuntimeError(
                    f"regen finalize failed: character not found; character_id={prep.character.id}"
                )

            cand.content = full_content
            cand.is_final = True
            cand.model_type = model_type

            turn.state = TurnState.OK.value

            chat.last_turn_id = turn.id
            chat.active_leaf_turn_id = turn.id
            chat.last_turn_at = now

            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise

    async def _finalize_regen_error(
        self,
        prep: _RegenPrep,
        code: str,
        message: str,
        partial_content: str,
    ) -> None:
        now = _now_utc()
        try:
            cand = await self._candidate_repo.get_by_id(prep.assistant_candidate_id)
            turn = await self._turn_repo.get_by_id(prep.assistant_turn_id)
            chat = await self._chat_repo.get_by_id(prep.chat_id)

            if cand:
                cand.content = partial_content or ""
                cand.is_final = True
                extra = dict(cand.extra or {})
                extra.update({"error_code": code, "error_message": message})
                cand.extra = extra

            if turn:
                turn.state = TurnState.ERROR.value

            if chat:
                chat.last_turn_id = prep.assistant_turn_id
                chat.active_leaf_turn_id = prep.assistant_turn_id
                chat.last_turn_at = now

            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise

    # ------------------------------------------------------------------
    # Continue generation
    # ------------------------------------------------------------------

    async def _walk_to_continuable_leaf(
        self, start: Turn, chat_id: uuid.UUID
    ) -> Turn:
        """Walk down from *start* to find the latest CHARACTER turn with no children.

        When a user rapidly clicks continue→cancel, the turn tree grows
        SYSTEM→CHARACTER(empty)→SYSTEM→CHARACTER(empty)→… beneath the
        original CHARACTER turn.  This helper finds the deepest CHARACTER
        leaf so ``prepare_continue`` can attach a new continuation there
        instead of hitting the ``turns_parent_candidate_uniq`` constraint.

        Raises 409 if the walk cannot find a continuable CHARACTER turn.
        """
        current = start
        while True:
            if current.primary_candidate_id is None:
                break
            child = await self._turn_repo.get_child_by_parent_candidate(
                current.primary_candidate_id
            )
            if child is None:
                break
            current = child

        if current.author_type == AuthorType.CHARACTER.value:
            return current

        raise APIError(
            status_code=409,
            code="resource_conflict",
            message=(
                "resource conflict: this turn already has a continuation; "
                f"turn_id={current.id}; chat_id={chat_id}"
            ),
        )

    async def prepare_continue(
        self,
        current_user: User,
        assistant_turn_id: uuid.UUID,
    ) -> _ContinuePrep:
        """Create SYSTEM turn (hidden, holds continue prompt) + CHARACTER turn placeholder."""
        try:
            source_turn = await self._turn_repo.get_for_update(assistant_turn_id)
            if not source_turn:
                raise APIError(
                    status_code=404,
                    code="turn_not_found",
                    message=f"resource not found: turn does not exist; turn_id={assistant_turn_id}",
                )

            if source_turn.author_type != AuthorType.CHARACTER.value:
                raise APIError(
                    status_code=409,
                    code="turn_author_conflict",
                    message=(
                        "resource conflict: continue is only supported for assistant turns; "
                        f"turn_id={source_turn.id}; author_type={source_turn.author_type}"
                    ),
                )

            chat = await self._chat_repo.get_for_update(source_turn.chat_id)
            if not chat:
                raise APIError(
                    status_code=404,
                    code="chat_not_found",
                    message=f"resource not found: chat does not exist; chat_id={source_turn.chat_id}",
                )

            if chat.user_id != current_user.id:
                raise APIError(
                    status_code=403,
                    code="chat_forbidden",
                    message=f"authorization failed: chat is not accessible; chat_id={chat.id}",
                )

            if chat.state != ChatState.ACTIVE.value:
                raise APIError(
                    status_code=409,
                    code="chat_state_conflict",
                    message=f"resource conflict: chat is not active; chat_id={chat.id}; state={chat.state}",
                )

            character = await self._character_repo.get_by_id(chat.character_id)
            if not character:
                raise APIError(
                    status_code=404,
                    code="character_not_found",
                    message=f"resource not found: character does not exist; character_id={chat.character_id}",
                )
            assert_character_chat_writable(character, user_id=current_user.id)

            if source_turn.primary_candidate_id:
                existing_child = await self._turn_repo.get_child_by_parent_candidate(
                    source_turn.primary_candidate_id
                )
                if existing_child:
                    source_turn = await self._walk_to_continuable_leaf(
                        existing_child, chat.id
                    )

            resolved_llm_route = await self._character_llm_service.resolve_runtime_route(character)

            now = _now_utc()
            last_no = int(chat.last_turn_no or 0)
            if chat.last_turn_no is None:
                last_no = await self._turn_repo.get_max_turn_no(chat_id=chat.id)
                chat.last_turn_no = last_no

            # SYSTEM turn
            system_turn_no = last_no + 1
            system_turn = Turn(
                chat_id=chat.id,
                turn_no=system_turn_no,
                parent_turn_id=source_turn.id,
                parent_candidate_id=source_turn.primary_candidate_id,
                author_type=AuthorType.SYSTEM.value,
                state=TurnState.OK.value,
                is_proactive=False,
            )
            self._turn_repo.add(system_turn)
            await self._uow.flush()

            system_cand = Candidate(
                turn_id=system_turn.id,
                candidate_no=1,
                content=CONTINUE_PROMPT,
                is_final=True,
                extra={"source": "continue_prompt"},
            )
            self._candidate_repo.add(system_cand)
            await self._uow.flush()
            system_turn.primary_candidate_id = system_cand.id

            # CHARACTER turn placeholder
            char_turn_no = system_turn_no + 1
            char_turn = Turn(
                chat_id=chat.id,
                turn_no=char_turn_no,
                parent_turn_id=system_turn.id,
                parent_candidate_id=system_cand.id,
                author_type=AuthorType.CHARACTER.value,
                author_character_id=character.id,
                state=TurnState.OK.value,
                is_proactive=False,
            )
            self._turn_repo.add(char_turn)
            await self._uow.flush()

            char_cand = Candidate(
                turn_id=char_turn.id,
                candidate_no=1,
                content="",
                is_final=False,
                extra={"source": "continue_placeholder"},
            )
            self._candidate_repo.add(char_cand)
            await self._uow.flush()
            char_turn.primary_candidate_id = char_cand.id

            chat.last_turn_no = char_turn_no
            if chat.origin == ChatOriginType.PROACTIVE.value:
                chat.last_read_turn_no = char_turn_no
            chat.last_turn_id = char_turn.id
            chat.active_leaf_turn_id = char_turn.id
            chat.last_turn_at = now

            # Extract last user text for memory search.
            last_user_text = ""
            if source_turn.parent_turn_id and source_turn.parent_candidate_id:
                parent_user_turn = await self._turn_repo.get_by_id(source_turn.parent_turn_id)
                if parent_user_turn and parent_user_turn.chat_id == chat.id:
                    parent_user_cand = await self._candidate_repo.get_by_id(
                        source_turn.parent_candidate_id
                    )
                    if parent_user_cand:
                        last_user_text = (parent_user_cand.content or "").strip()

            history_leaf_turn_id = source_turn.id

            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise

        return _ContinuePrep(
            chat_id=chat.id,
            character=character,
            resolved_llm_route=resolved_llm_route,
            source_assistant_turn_id=source_turn.id,
            system_turn_id=system_turn.id,
            system_candidate_id=system_cand.id,
            assistant_turn_id=char_turn.id,
            assistant_candidate_id=char_cand.id,
            history_leaf_turn_id=history_leaf_turn_id,
            last_user_text=last_user_text,
        )

    async def stream_continue_with_prep(
        self,
        prep: _ContinuePrep,
        current_user: User,
    ) -> AsyncGenerator[str, None]:
        yield _sse_event(
            {
                "type": "meta",
                "assistant_turn": {
                    "id": str(prep.assistant_turn_id),
                    "candidate_id": str(prep.assistant_candidate_id),
                    "candidate_no": 1,
                },
            }
        )

        accumulated = ""
        tts_enabled = False
        tts_provider: str | None = None
        tts_provider_voice_id: str | None = None
        tts_provider_model: str | None = None
        tts_voice_source_type: str | None = None
        tts_segmenter: ConversationTTSSegmenter | None = None
        tts_worker_done = False
        tts_error_emitted = False
        tts_segments_queue: asyncio.Queue[str | None] | None = None
        tts_events_queue: asyncio.Queue[dict[str, Any]] | None = None
        tts_worker_task: asyncio.Task[None] | None = None
        post_done_tasks: PostDoneTailTasks | None = None
        workflow_context = start_workflow_observation(
            name="chat.turn.continue",
            input_payload={
                "chat_id": str(prep.chat_id),
                "assistant_turn_id": str(prep.assistant_turn_id),
                "assistant_candidate_id": str(prep.assistant_candidate_id),
                "source_assistant_turn_id": str(prep.source_assistant_turn_id),
            },
            user_id=str(current_user.id),
            session_id=f"chat:{prep.chat_id}",
            tags=["chat", "turn", "continue"],
            metadata={
                "workflow": "chat.reply.continue",
                "chat_id": str(prep.chat_id),
                "character_id": str(prep.character.id),
                "assistant_candidate_id": str(prep.assistant_candidate_id),
            },
            manual=True,
        )
        workflow_observation = workflow_context.__enter__()
        try:
            workflow_trace_context = current_trace_context(workflow_observation)
            if workflow_trace_context is not None:
                await self._upsert_candidate_extra(
                    candidate_id=prep.assistant_candidate_id,
                    payload={"observability_context": workflow_trace_context},
                    commit=True,
                )
            user_settings = await self._user_settings_repo.get_or_create(
                current_user.id,
                default_font_size=app_settings.USER_SETTINGS_MESSAGE_FONT_SIZE_DEFAULT,
                default_display_mode=app_settings.USER_SETTINGS_DISPLAY_MODE_DEFAULT,
                default_memory_enabled=True,
                default_reply_card_enabled=app_settings.USER_SETTINGS_REPLY_CARD_ENABLED_DEFAULT,
                default_mixed_input_auto_translate_enabled=(
                    app_settings.USER_SETTINGS_MIXED_INPUT_AUTO_TRANSLATE_ENABLED_DEFAULT
                ),
                default_auto_read_aloud_enabled=(
                    app_settings.USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT
                ),
                default_preferred_expression_bias_enabled=(
                    app_settings.USER_SETTINGS_PREFERRED_EXPRESSION_BIAS_ENABLED_DEFAULT
                ),
                default_proactive_enabled=app_settings.USER_SETTINGS_PROACTIVE_ENABLED_DEFAULT,
                default_timezone=app_settings.USER_SETTINGS_TIMEZONE_DEFAULT,
            )
            memory_active = await self._subscription_service.has_feature(
                current_user, "memory_feature"
            ) and bool(user_settings.memory_enabled)
            tts_enabled = bool(self._voice_service and user_settings.auto_read_aloud_enabled)
            if tts_enabled and prep.character.voice_provider_voice_id.strip():
                tts_provider = prep.character.voice_provider
                tts_provider_voice_id = prep.character.voice_provider_voice_id
                tts_provider_model = prep.character.voice_model
                tts_voice_source_type = prep.character.voice_source_type
            if tts_enabled:
                self._schedule_turn_tts_warmup(
                    operation="chat.reply.continue",
                    task_name="turn.continue.tts_warmup",
                    chat_id=prep.chat_id,
                    turn_id=prep.assistant_turn_id,
                    candidate_id=prep.assistant_candidate_id,
                    character=prep.character,
                )

            def _consume_tts_event(event: dict[str, Any]) -> str | None:
                nonlocal tts_worker_done
                nonlocal tts_error_emitted
                event_type = event.get("type")
                if event_type == "tts_worker_done":
                    tts_worker_done = True
                    return None
                if event_type == "tts_error":
                    tts_error_emitted = True
                return _sse_event(event)

            def _drain_tts_frames() -> list[str]:
                frames: list[str] = []
                if not tts_events_queue:
                    return frames
                while True:
                    try:
                        event = tts_events_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    frame = _consume_tts_event(event)
                    if frame:
                        frames.append(frame)
                return frames

            async def _wait_tts_frame(timeout_s: float) -> str | None:
                if not tts_events_queue:
                    return None
                try:
                    event = await asyncio.wait_for(tts_events_queue.get(), timeout=timeout_s)
                except TimeoutError:
                    return None
                return _consume_tts_event(event)

            async def _drain_post_done_frames() -> list[str]:
                if post_done_tasks is None:
                    return []

                frames: list[str] = []
                for item in post_done_tasks.drain_nowait():
                    frames.extend(
                        await self._render_post_done_item(
                            item=item,
                            current_user=current_user,
                            assistant_candidate_id=prep.assistant_candidate_id,
                            persistence_log_message="Continue reply card persistence failed",
                        )
                    )
                return frames

            if tts_enabled:
                tts_segments_queue = asyncio.Queue()
                tts_events_queue = asyncio.Queue()
                tts_segmenter = ConversationTTSSegmenter()

                async def _tts_worker() -> None:
                    assert self._voice_service is not None
                    assert tts_segments_queue is not None
                    assert tts_events_queue is not None
                    try:
                        async for event in self._voice_service.stream_tts_sse_events_from_segments(
                            segments_queue=tts_segments_queue,
                            assistant_candidate_id=str(prep.assistant_candidate_id),
                            emit_done=False,
                            provider=tts_provider or prep.character.voice_provider,
                            provider_voice_id=tts_provider_voice_id,
                            provider_model=tts_provider_model,
                            voice_source_type=tts_voice_source_type
                            or prep.character.voice_source_type,
                        ):
                            await tts_events_queue.put(event)
                    except APIError as exc:
                        await tts_events_queue.put(
                            {
                                "type": "tts_error",
                                "code": exc.code,
                                "message": exc.message,
                            }
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.exception("Continue TTS worker failed: %s", exc)
                        await tts_events_queue.put(
                            {
                                "type": "tts_error",
                                "code": "tts_upstream_error",
                                "message": (
                                    "upstream error: tts synthesis failed during continue stream"
                                ),
                            }
                        )
                    finally:
                        await tts_events_queue.put({"type": "tts_worker_done"})

                tts_worker_task = asyncio.create_task(
                    _tts_worker(),
                    name="turn.continue.tts_worker",
                )

            history_messages = await _load_llm_history_messages_from_leaf(
                turn_repo=self._turn_repo,
                candidate_repo=self._candidate_repo,
                chat_id=prep.chat_id,
                leaf_turn_id=prep.history_leaf_turn_id,
                limit=app_settings.CHAT_HISTORY_LIMIT,
            )

            memory_query = prep.last_user_text or CONTINUE_PROMPT
            relevant_memories: dict[str, list[MemoryRecord]] = {"episodic": [], "semantic": []}
            if memory_active:
                relevant_memories = await safe_memory_search(
                    self._memory_repo,
                    query=memory_query,
                    user_id=str(current_user.id),
                    character_id=str(prep.character.id),
                    chat_id=str(prep.chat_id),
                    timeout_s=self._memory_repo.allowed_memory_search_timeout_s,
                )
            preferred_expressions: list[str] = []
            if user_settings.preferred_expression_bias_enabled:
                preferred_expressions = (
                    await self._chat_learning_service.load_preferred_expressions(
                        user_id=current_user.id,
                    )
                )

            generation_context = ChatGenerationContext(
                character_system_prompt=prep.character.system_prompt,
                history_messages=history_messages,
                user_message=CONTINUE_PROMPT,
                memories=relevant_memories,
                use_all_semantic=(
                    self._memory_repo.use_all_semantic if memory_active else False
                ),
                preferred_expressions=preferred_expressions,
                memory_active=memory_active,
                channel_policy="chat",
            )
            messages = build_generation_messages(
                generation_context,
                user_message_builder=_build_user_message_with_images,
            )
            async for chunk in self._character_llm_service.chat_stream_messages(
                messages=messages,
                route=prep.resolved_llm_route,
                operation="chat.reply.continue",
                route_role="chat",
                trace_context=workflow_trace_context,
                user_id=str(current_user.id),
                chat_id=str(prep.chat_id),
                turn_id=str(prep.assistant_turn_id),
                candidate_id=str(prep.assistant_candidate_id),
                character_id=str(prep.character.id),
            ):
                accumulated += chunk
                yield _sse_event({"type": "chunk", "content": chunk})
                if tts_enabled and tts_segments_queue is not None and tts_segmenter is not None:
                    for segment in tts_segmenter.feed(chunk):
                        await tts_segments_queue.put(segment)
                for frame in _drain_tts_frames():
                    yield frame

            await self._finalize_continue_success(
                prep=prep,
                full_content=accumulated,
                model_type=prep.resolved_llm_route.model_type_label,
            )
            if tts_enabled and tts_segments_queue is not None and tts_segmenter is not None:
                for tail_segment in tts_segmenter.flush():
                    await tts_segments_queue.put(tail_segment)
                await tts_segments_queue.put(None)
                for frame in _drain_tts_frames():
                    yield frame

            yield _sse_event(
                {
                    "type": "done",
                    "full_content": accumulated,
                    "assistant_turn_id": str(prep.assistant_turn_id),
                    "assistant_candidate_id": str(prep.assistant_candidate_id),
                }
            )

            chat_history_for_suggestions = (
                history_messages + [{"role": "user", "content": CONTINUE_PROMPT}]
            )[-app_settings.CHAT_HISTORY_LIMIT :]
            should_generate_card = (
                user_settings.reply_card_enabled or user_settings.display_mode == "detailed"
            )
            if should_generate_card:
                yield _sse_event(
                    {
                        "type": "reply_card_started",
                        "assistant_candidate_id": str(prep.assistant_candidate_id),
                    }
                )

            post_done_tasks = start_post_done_tail_tasks(
                assistant_candidate_id=str(prep.assistant_candidate_id),
                suggestions_coro=self._chat_learning_service.generate_reply_suggestions(
                    chat_history=chat_history_for_suggestions,
                    assistant_latest_message=accumulated,
                    preferred_expressions=preferred_expressions,
                    route=prep.resolved_llm_route,
                    trace_context=workflow_trace_context,
                    user_id=str(current_user.id),
                    chat_id=str(prep.chat_id),
                    turn_id=str(prep.assistant_turn_id),
                    candidate_id=str(prep.assistant_candidate_id),
                    character_id=str(prep.character.id),
                ),
                logger=logger,
                suggestions_error_log_message="Continue reply suggestions generation failed",
                reply_card_core_coro=(
                    self._chat_learning_service.generate_reply_card(
                        assistant_text=accumulated,
                        trace_context=workflow_trace_context,
                        user_id=str(current_user.id),
                        chat_id=str(prep.chat_id),
                        turn_id=str(prep.assistant_turn_id),
                        candidate_id=str(prep.assistant_candidate_id),
                        character_id=str(prep.character.id),
                    )
                    if should_generate_card
                    else None
                ),
                reply_card_error_log_message="Continue reply card generation failed",
            )

            if tts_enabled and tts_worker_task is not None:
                deadline = time.monotonic() + 60.0
                while not tts_worker_done:
                    for frame in _drain_tts_frames():
                        yield frame
                    for frame in await _drain_post_done_frames():
                        yield frame

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        tts_error_emitted = True
                        yield _sse_event(
                            {
                                "type": "tts_error",
                                "code": "external_timeout",
                                "message": "upstream timeout: tts synthesis timed out",
                            }
                        )
                        break

                    if post_done_tasks is not None and post_done_tasks.pending:
                        next_item = await post_done_tasks.wait_for_item(min(0.05, remaining))
                        if next_item is not None:
                            for frame in await self._render_post_done_item(
                                item=next_item,
                                current_user=current_user,
                                assistant_candidate_id=prep.assistant_candidate_id,
                                persistence_log_message="Continue reply card persistence failed",
                            ):
                                yield frame
                            continue

                    next_frame = await _wait_tts_frame(min(0.05, remaining))
                    if next_frame:
                        yield next_frame
                    elif tts_worker_task.done():
                        for frame in _drain_tts_frames():
                            yield frame
                        if not tts_worker_done:
                            tts_worker_done = True

                if tts_worker_task.done():
                    try:
                        await tts_worker_task
                    except Exception:
                        if not tts_error_emitted:
                            tts_error_emitted = True
                            yield _sse_event(
                                {
                                    "type": "tts_error",
                                    "code": "tts_upstream_error",
                                    "message": "upstream error: tts synthesis failed",
                                }
                            )

                if tts_worker_done and not tts_error_emitted:
                    yield _sse_event(
                        {
                            "type": "tts_audio_done",
                            "assistant_candidate_id": str(prep.assistant_candidate_id),
                        }
                    )

            if post_done_tasks is not None:
                while post_done_tasks.pending:
                    for frame in await _drain_post_done_frames():
                        yield frame
                    if not post_done_tasks.pending:
                        break
                    next_item = await post_done_tasks.wait_for_item(0.05)
                    if next_item is None:
                        continue
                    for frame in await self._render_post_done_item(
                        item=next_item,
                        current_user=current_user,
                        assistant_candidate_id=prep.assistant_candidate_id,
                        persistence_log_message="Continue reply card persistence failed",
                    ):
                        yield frame

            if memory_active:
                spawn_bg_task(
                    self._memory_repo.manage(
                        user_text=memory_query,
                        assistant_text=accumulated,
                        user_id=str(current_user.id),
                        character_id=str(prep.character.id),
                        chat_id=str(prep.chat_id),
                    ),
                    name="memory.manage_async",
                    ctx={
                        "user_id": str(current_user.id),
                        "character_id": str(prep.character.id),
                        "chat_id": str(prep.chat_id),
                    },
                )
                episodic_for_prompt = relevant_memories.get("episodic", [])[:5]
                spawn_bg_task(
                    self._memory_repo.judge_and_assign_narrative_group(
                        episodic_memories=episodic_for_prompt,
                        last_user=memory_query,
                        last_assistant=accumulated,
                        user_id=str(current_user.id),
                        character_id=str(prep.character.id),
                        chat_id=str(prep.chat_id),
                    ),
                    name="memory.judge_and_assign_narrative_group_async",
                    ctx={
                        "user_id": str(current_user.id),
                        "character_id": str(prep.character.id),
                        "chat_id": str(prep.chat_id),
                    },
                )

            update_observation_safe(
                workflow_observation,
                output={
                    "status": "completed",
                    "assistant_candidate_id": str(prep.assistant_candidate_id),
                    "content_length": len(accumulated),
                    "memory_active": memory_active,
                    "tts_enabled": tts_enabled,
                },
            )

        except LLMFirstChunkTimeoutError as e:
            message = (
                "upstream timeout: llm stream did not produce the first chunk in time; "
                f"model={e.model}; timeout_s={e.timeout_s:.1f}"
            )
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "llm_stream_first_chunk_timeout"},
            )
            await self._rollback_after_stream_exception(reason="llm_stream_first_chunk_timeout")
            await self._finalize_continue_error(
                prep=prep,
                code="llm_stream_first_chunk_timeout",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event(
                {
                    "type": "error",
                    "code": "llm_stream_first_chunk_timeout",
                    "message": message,
                }
            )
        except LLMAuthenticationError as e:
            message = e.public_message()
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "llm_service_error"},
            )
            await self._rollback_after_stream_exception(reason="llm_service_error")
            await self._finalize_continue_error(
                prep=prep,
                code="llm_service_error",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "llm_service_error", "message": message})
        except LLMCallError as e:
            message = (
                "upstream error: llm request failed after retries; "
                f"model={e.model}; attempts={e.attempts}"
            )
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "llm_service_error"},
            )
            await self._rollback_after_stream_exception(reason="llm_service_error")
            await self._finalize_continue_error(
                prep=prep,
                code="llm_service_error",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "llm_service_error", "message": message})
        except Exception as e:
            logger.exception("Continue stream error for user %s: %s", current_user.id, e)
            message = "continue stream failed: unexpected streaming error"
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "continue_stream_failed"},
            )
            await self._rollback_after_stream_exception(reason="continue_stream_failed")
            await self._finalize_continue_error(
                prep=prep,
                code="continue_stream_failed",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "continue_stream_failed", "message": message})
        finally:
            if post_done_tasks is not None:
                await post_done_tasks.aclose()
            if tts_worker_task and not tts_worker_task.done():
                tts_worker_task.cancel()
            workflow_context.__exit__(*sys.exc_info())

    async def _finalize_continue_success(
        self, prep: _ContinuePrep, full_content: str, model_type: str
    ) -> None:
        now = _now_utc()
        try:
            cand = await self._candidate_repo.get_by_id(prep.assistant_candidate_id)
            turn = await self._turn_repo.get_by_id(prep.assistant_turn_id)
            chat = await self._chat_repo.get_by_id(prep.chat_id)
            character = await self._character_repo.get_by_id(prep.character.id)
            if not cand:
                raise RuntimeError(
                    f"continue finalize failed: candidate not found; candidate_id={prep.assistant_candidate_id}"
                )
            if not turn:
                raise RuntimeError(
                    f"continue finalize failed: turn not found; turn_id={prep.assistant_turn_id}"
                )
            if not chat:
                raise RuntimeError(f"continue finalize failed: chat not found; chat_id={prep.chat_id}")
            if not character:
                raise RuntimeError(
                    f"continue finalize failed: character not found; character_id={prep.character.id}"
                )

            cand.content = full_content
            cand.is_final = True
            cand.model_type = model_type

            turn.state = TurnState.OK.value

            chat.last_turn_id = turn.id
            chat.active_leaf_turn_id = turn.id
            chat.last_turn_at = now

            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise

    async def _finalize_continue_error(
        self,
        prep: _ContinuePrep,
        code: str,
        message: str,
        partial_content: str,
    ) -> None:
        now = _now_utc()
        try:
            cand = await self._candidate_repo.get_by_id(prep.assistant_candidate_id)
            turn = await self._turn_repo.get_by_id(prep.assistant_turn_id)
            chat = await self._chat_repo.get_by_id(prep.chat_id)

            if cand:
                cand.content = partial_content or ""
                cand.is_final = True
                extra = dict(cand.extra or {})
                extra.update({"error_code": code, "error_message": message})
                cand.extra = extra

            if turn:
                turn.state = TurnState.ERROR.value

            if chat:
                chat.last_turn_id = prep.assistant_turn_id
                chat.active_leaf_turn_id = prep.assistant_turn_id
                chat.last_turn_at = now

            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise

    async def prepare_user_edit_stream(
        self,
        current_user: User,
        user_turn_id: uuid.UUID,
        content: str,
        images: list[ChatImageRef] | None = None,
    ) -> _UserEditPrep:
        """Validate and create the new user candidate + new assistant turn placeholder before SSE starts."""
        normalized_content = (content or "").strip()
        if not normalized_content:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: edited content cannot be empty",
            )
        normalized_images = _normalize_requested_chat_images(
            images,
            owner_id=str(current_user.id),
        )

        try:
            user_turn = await self._turn_repo.get_for_update(user_turn_id)
            if not user_turn:
                raise APIError(
                    status_code=404,
                    code="turn_not_found",
                    message=f"resource not found: turn does not exist; turn_id={user_turn_id}",
                )

            if user_turn.author_type != AuthorType.USER.value:
                raise APIError(
                    status_code=409,
                    code="turn_author_conflict",
                    message=(
                        "resource conflict: edit is only supported for user turns; "
                        f"turn_id={user_turn.id}; author_type={user_turn.author_type}"
                    ),
                )

            chat = await self._chat_repo.get_for_update(user_turn.chat_id)
            if not chat:
                raise APIError(
                    status_code=404,
                    code="chat_not_found",
                    message=f"resource not found: chat does not exist; chat_id={user_turn.chat_id}",
                )

            if chat.user_id != current_user.id:
                raise APIError(
                    status_code=403,
                    code="chat_forbidden",
                    message=f"authorization failed: chat is not accessible; chat_id={chat.id}",
                )

            if chat.state != ChatState.ACTIVE.value:
                raise APIError(
                    status_code=409,
                    code="chat_state_conflict",
                    message=f"resource conflict: chat is not active; chat_id={chat.id}; state={chat.state}",
                )

            character = await self._character_repo.get_by_id(chat.character_id)
            if not character:
                raise APIError(
                    status_code=404,
                    code="character_not_found",
                    message=f"resource not found: character does not exist; character_id={chat.character_id}",
                )
            assert_character_chat_writable(character, user_id=current_user.id)
            resolved_llm_route = await self._character_llm_service.resolve_runtime_route(character)

            max_no = await self._candidate_repo.get_max_candidate_no(turn_id=user_turn.id)
            if max_no >= 10:
                raise APIError(
                    status_code=409,
                    code="turn_candidate_limit_reached",
                    message=f"resource conflict: candidate limit reached; turn_id={user_turn.id}; limit=10",
                )

            new_user_no = max_no + 1
            now = _now_utc()

            # If the client did not pass images at all (None, not []), carry over
            # images from the earliest candidate that has them, so editing text does
            # not drop attachments across repeated edits.  Explicit `[]` clears them.
            if images is None and not normalized_images:
                all_cands = await self._candidate_repo.list_by_turn_id(turn_id=user_turn_id)
                for cand in sorted(all_cands, key=lambda c: c.candidate_no):
                    chat_images_raw = (cand.extra or {}).get("chat_images")
                    original_refs = _materialize_chat_image_refs(chat_images_raw)
                    logger.info(
                        "Edit image carry-over scan.",
                        extra={
                            "log_module": "chat",
                            "event": "chat.image.edit_carry_over_scan",
                            "operation": "chat.reply.edit",
                            "turn_id": str(user_turn_id),
                            "scanned_candidate_id": str(cand.id),
                            "scanned_candidate_no": cand.candidate_no,
                            "has_images": original_refs is not None,
                            "carried_over_count": len(original_refs or []),
                            "carried_over_keys": [r.image_key for r in (original_refs or [])],
                        },
                    )
                    if original_refs:
                        normalized_images = tuple(original_refs)
                        break
            elif images is not None:
                logger.info(
                    "Edit images from request (no carry-over).",
                    extra={
                        "log_module": "chat",
                        "event": "chat.image.edit_from_request",
                        "operation": "chat.reply.edit",
                        "turn_id": str(user_turn_id),
                        "normalized_count": len(normalized_images),
                        "normalized_keys": [r.image_key for r in normalized_images],
                    },
                )

            cand_extra: dict[str, Any] = {"source": "user_edit"}
            if normalized_images:
                cand_extra["chat_images"] = [
                    {"image_key": img.image_key, "data_url": img.data_url}
                    for img in normalized_images
                ]
            user_cand = Candidate(
                turn_id=user_turn.id,
                candidate_no=new_user_no,
                content=normalized_content,
                is_final=True,
                extra=cand_extra,
            )
            self._candidate_repo.add(user_cand)
            await self._uow.flush()

            user_turn.primary_candidate_id = user_cand.id
            user_turn.state = TurnState.OK.value

            # Allocate a new assistant turn_no (prefer chats.last_turn_no).
            last_no = int(chat.last_turn_no or 0)
            if chat.last_turn_no is None:
                last_no = await self._turn_repo.get_max_turn_no(chat_id=chat.id)
                chat.last_turn_no = last_no

            assistant_turn_no = last_no + 1

            assistant_turn = Turn(
                chat_id=chat.id,
                turn_no=assistant_turn_no,
                parent_turn_id=user_turn.id,
                parent_candidate_id=user_cand.id,
                author_type=AuthorType.CHARACTER.value,
                author_character_id=character.id,
                state=TurnState.OK.value,
                is_proactive=False,
            )
            self._turn_repo.add(assistant_turn)
            await self._uow.flush()

            assistant_cand = Candidate(
                turn_id=assistant_turn.id,
                candidate_no=1,
                content="",
                is_final=False,
                extra={"source": "stream_placeholder"},
            )
            self._candidate_repo.add(assistant_cand)
            await self._uow.flush()
            assistant_turn.primary_candidate_id = assistant_cand.id

            chat.last_turn_no = assistant_turn_no
            if chat.origin == ChatOriginType.PROACTIVE.value:
                chat.last_read_turn_no = assistant_turn_no
            chat.last_turn_id = assistant_turn.id
            chat.active_leaf_turn_id = assistant_turn.id
            chat.last_turn_at = now

            history_leaf_turn_id = user_turn.parent_turn_id

            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise

        return _UserEditPrep(
            chat_id=chat.id,
            character=character,
            resolved_llm_route=resolved_llm_route,
            history_leaf_turn_id=history_leaf_turn_id,
            user_turn_id=user_turn.id,
            user_candidate_id=user_cand.id,
            user_candidate_no=new_user_no,
            user_text=normalized_content,
            assistant_turn_id=assistant_turn.id,
            assistant_turn_no=assistant_turn_no,
            assistant_candidate_id=assistant_cand.id,
            chat_images=normalized_images,
        )

    async def stream_user_edit_with_prep(
        self,
        prep: _UserEditPrep,
        current_user: User,
    ) -> AsyncGenerator[str, None]:
        yield _sse_event(
            {
                "type": "meta",
                "user_turn": {
                    "id": str(prep.user_turn_id),
                    "candidate_id": str(prep.user_candidate_id),
                    "candidate_no": prep.user_candidate_no,
                },
                "assistant_turn": {
                    "id": str(prep.assistant_turn_id),
                    "turn_no": prep.assistant_turn_no,
                    "candidate_id": str(prep.assistant_candidate_id),
                },
            }
        )

        accumulated = ""
        outbound_user_content = prep.user_text
        tts_enabled = False
        tts_provider: str | None = None
        tts_provider_voice_id: str | None = None
        tts_provider_model: str | None = None
        tts_voice_source_type: str | None = None
        tts_segmenter: ConversationTTSSegmenter | None = None
        tts_worker_done = False
        tts_error_emitted = False
        tts_segments_queue: asyncio.Queue[str | None] | None = None
        tts_events_queue: asyncio.Queue[dict[str, Any]] | None = None
        tts_worker_task: asyncio.Task[None] | None = None
        post_done_tasks: PostDoneTailTasks | None = None
        workflow_context = start_workflow_observation(
            name="chat.turn.edit",
            input_payload={
                "chat_id": str(prep.chat_id),
                "user_turn_id": str(prep.user_turn_id),
                "assistant_turn_id": str(prep.assistant_turn_id),
                "assistant_candidate_id": str(prep.assistant_candidate_id),
                "content_length": len(prep.user_text),
            },
            user_id=str(current_user.id),
            session_id=f"chat:{prep.chat_id}",
            tags=["chat", "turn", "edit"],
            metadata={
                "workflow": "chat.reply.edit",
                "chat_id": str(prep.chat_id),
                "character_id": str(prep.character.id),
                "assistant_candidate_id": str(prep.assistant_candidate_id),
            },
            manual=True,
        )
        workflow_observation = workflow_context.__enter__()
        try:
            workflow_trace_context = current_trace_context(workflow_observation)
            if workflow_trace_context is not None:
                await self._upsert_candidate_extra(
                    candidate_id=prep.user_candidate_id,
                    payload={"observability_context": workflow_trace_context},
                )
                await self._upsert_candidate_extra(
                    candidate_id=prep.assistant_candidate_id,
                    payload={"observability_context": workflow_trace_context},
                    commit=True,
                )
            user_settings = await self._user_settings_repo.get_or_create(
                current_user.id,
                default_font_size=app_settings.USER_SETTINGS_MESSAGE_FONT_SIZE_DEFAULT,
                default_display_mode=app_settings.USER_SETTINGS_DISPLAY_MODE_DEFAULT,
                default_memory_enabled=True,
                default_reply_card_enabled=app_settings.USER_SETTINGS_REPLY_CARD_ENABLED_DEFAULT,
                default_mixed_input_auto_translate_enabled=(
                    app_settings.USER_SETTINGS_MIXED_INPUT_AUTO_TRANSLATE_ENABLED_DEFAULT
                ),
                default_auto_read_aloud_enabled=(
                    app_settings.USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT
                ),
                default_preferred_expression_bias_enabled=(
                    app_settings.USER_SETTINGS_PREFERRED_EXPRESSION_BIAS_ENABLED_DEFAULT
                ),
                default_proactive_enabled=app_settings.USER_SETTINGS_PROACTIVE_ENABLED_DEFAULT,
                default_timezone=app_settings.USER_SETTINGS_TIMEZONE_DEFAULT,
            )
            memory_active = await self._subscription_service.has_feature(
                current_user, "memory_feature"
            ) and bool(user_settings.memory_enabled)
            tts_enabled = bool(self._voice_service and user_settings.auto_read_aloud_enabled)
            if tts_enabled and prep.character.voice_provider_voice_id.strip():
                tts_provider = prep.character.voice_provider
                tts_provider_voice_id = prep.character.voice_provider_voice_id
                tts_provider_model = prep.character.voice_model
                tts_voice_source_type = prep.character.voice_source_type
            if tts_enabled:
                self._schedule_turn_tts_warmup(
                    operation="chat.reply.edit",
                    task_name="turn.user_edit.tts_warmup",
                    chat_id=prep.chat_id,
                    turn_id=prep.assistant_turn_id,
                    candidate_id=prep.assistant_candidate_id,
                    character=prep.character,
                )

            def _consume_tts_event(event: dict[str, Any]) -> str | None:
                nonlocal tts_worker_done
                nonlocal tts_error_emitted
                event_type = event.get("type")
                if event_type == "tts_worker_done":
                    tts_worker_done = True
                    return None
                if event_type == "tts_error":
                    tts_error_emitted = True
                return _sse_event(event)

            def _drain_tts_frames() -> list[str]:
                frames: list[str] = []
                if not tts_events_queue:
                    return frames
                while True:
                    try:
                        event = tts_events_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    frame = _consume_tts_event(event)
                    if frame:
                        frames.append(frame)
                return frames

            async def _wait_tts_frame(timeout_s: float) -> str | None:
                if not tts_events_queue:
                    return None
                try:
                    event = await asyncio.wait_for(tts_events_queue.get(), timeout=timeout_s)
                except TimeoutError:
                    return None
                return _consume_tts_event(event)

            async def _drain_post_done_frames() -> list[str]:
                if post_done_tasks is None:
                    return []

                frames: list[str] = []
                for item in post_done_tasks.drain_nowait():
                    frames.extend(
                        await self._render_post_done_item(
                            item=item,
                            current_user=current_user,
                            assistant_candidate_id=prep.assistant_candidate_id,
                            persistence_log_message="User-edit reply card persistence failed",
                        )
                    )
                return frames

            if tts_enabled:
                tts_segments_queue = asyncio.Queue()
                tts_events_queue = asyncio.Queue()
                tts_segmenter = ConversationTTSSegmenter()

                async def _tts_worker() -> None:
                    assert self._voice_service is not None
                    assert tts_segments_queue is not None
                    assert tts_events_queue is not None
                    try:
                        async for event in self._voice_service.stream_tts_sse_events_from_segments(
                            segments_queue=tts_segments_queue,
                            assistant_candidate_id=str(prep.assistant_candidate_id),
                            emit_done=False,
                            provider=tts_provider or prep.character.voice_provider,
                            provider_voice_id=tts_provider_voice_id,
                            provider_model=tts_provider_model,
                            voice_source_type=tts_voice_source_type
                            or prep.character.voice_source_type,
                        ):
                            await tts_events_queue.put(event)
                    except APIError as exc:
                        await tts_events_queue.put(
                            {
                                "type": "tts_error",
                                "code": exc.code,
                                "message": exc.message,
                            }
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.exception("User-edit TTS worker failed: %s", exc)
                        await tts_events_queue.put(
                            {
                                "type": "tts_error",
                                "code": "tts_upstream_error",
                                "message": (
                                    "upstream error: tts synthesis failed during user-edit stream"
                                ),
                            }
                        )
                    finally:
                        await tts_events_queue.put({"type": "tts_worker_done"})

                tts_worker_task = asyncio.create_task(
                    _tts_worker(),
                    name="turn.user_edit.tts_worker",
                )

            if (
                user_settings.mixed_input_auto_translate_enabled
                and self._chat_learning_service.contains_chinese(prep.user_text)
            ):
                translated = prep.user_text
                try:
                    translated = await self._chat_learning_service.translate_mixed_to_english(
                        prep.user_text,
                        trace_context=workflow_trace_context,
                        user_id=str(current_user.id),
                        chat_id=str(prep.chat_id),
                        turn_id=str(prep.user_turn_id),
                        character_id=str(prep.character.id),
                    )
                except Exception:
                    logger.exception(
                        "User-edit mixed-input translation failed; fallback to original content"
                    )
                    translated = prep.user_text

                outbound_user_content = translated or prep.user_text
                transform_payload = {
                    "applied": bool(
                        outbound_user_content.strip() and outbound_user_content != prep.user_text
                    ),
                    "transformed_content": outbound_user_content,
                }
                await self._upsert_candidate_extra(
                    candidate_id=prep.user_candidate_id,
                    payload={"input_transform": transform_payload},
                )
                yield _sse_event(
                    {
                        "type": "transform_done",
                        "original_content": prep.user_text,
                        "transformed_content": outbound_user_content,
                        "applied": transform_payload["applied"],
                    }
                )

            history_messages = await _load_llm_history_messages_from_leaf(
                turn_repo=self._turn_repo,
                candidate_repo=self._candidate_repo,
                chat_id=prep.chat_id,
                leaf_turn_id=prep.history_leaf_turn_id,
                limit=app_settings.CHAT_HISTORY_LIMIT,
            )

            relevant_memories: dict[str, list[MemoryRecord]] = {"episodic": [], "semantic": []}
            if memory_active:
                relevant_memories = await safe_memory_search(
                    self._memory_repo,
                    query=outbound_user_content,
                    user_id=str(current_user.id),
                    character_id=str(prep.character.id),
                    chat_id=str(prep.chat_id),
                    timeout_s=self._memory_repo.allowed_memory_search_timeout_s,
                )
            preferred_expressions = []
            if user_settings.preferred_expression_bias_enabled:
                preferred_expressions = (
                    await self._chat_learning_service.load_preferred_expressions(
                        user_id=current_user.id,
                    )
                )

            generation_context = ChatGenerationContext(
                character_system_prompt=prep.character.system_prompt,
                history_messages=history_messages,
                user_message=outbound_user_content,
                memories=relevant_memories,
                use_all_semantic=(
                    self._memory_repo.use_all_semantic if memory_active else False
                ),
                preferred_expressions=preferred_expressions,
                chat_images=prep.chat_images,
                memory_active=memory_active,
                channel_policy="chat",
            )
            messages = build_generation_messages(
                generation_context,
                user_message_builder=_build_user_message_with_images,
            )

            # Diagnostic: log whether the final user message includes images.
            last_msg = messages[-1]
            last_content = last_msg.get("content")
            has_image_parts = (
                isinstance(last_content, list)
                and any(isinstance(p, dict) and p.get("type") == "image_url" for p in last_content)
            )
            logger.info(
                "Edit stream messages assembled.",
                extra={
                    "log_module": "chat",
                    "event": "chat.image.edit_messages_assembled",
                    "operation": "chat.reply.edit",
                    "chat_id": str(prep.chat_id),
                    "assistant_turn_id": str(prep.assistant_turn_id),
                    "prep_chat_images_count": len(prep.chat_images),
                    "prep_chat_images_keys": [r.image_key for r in prep.chat_images],
                    "final_user_message_has_images": has_image_parts,
                },
            )

            has_multimodal_context = bool(prep.chat_images) or any(
                _message_has_image_parts(m) for m in history_messages
            )
            active_route = (
                self._character_llm_service.resolve_vision_route()
                if has_multimodal_context
                else prep.resolved_llm_route
            )
            logger.info(
                "Edit stream route selected.",
                extra={
                    "log_module": "chat",
                    "event": "chat.image.edit_route_selected",
                    "operation": "chat.reply.edit",
                    "chat_id": str(prep.chat_id),
                    "has_multimodal_context": has_multimodal_context,
                    "model": active_route.model if active_route else None,
                },
            )

            async for chunk in self._character_llm_service.chat_stream_messages(
                messages=messages,
                route=active_route,
                operation="chat.reply.edit",
                route_role="chat",
                trace_context=workflow_trace_context,
                user_id=str(current_user.id),
                chat_id=str(prep.chat_id),
                turn_id=str(prep.assistant_turn_id),
                candidate_id=str(prep.assistant_candidate_id),
                character_id=str(prep.character.id),
            ):
                accumulated += chunk
                yield _sse_event({"type": "chunk", "content": chunk})
                if tts_enabled and tts_segments_queue is not None and tts_segmenter is not None:
                    for segment in tts_segmenter.feed(chunk):
                        await tts_segments_queue.put(segment)
                for frame in _drain_tts_frames():
                    yield frame

            await self._finalize_user_edit_success(
                prep=prep,
                full_content=accumulated,
                model_type=prep.resolved_llm_route.model_type_label,
            )
            if tts_enabled and tts_segments_queue is not None and tts_segmenter is not None:
                for tail_segment in tts_segmenter.flush():
                    await tts_segments_queue.put(tail_segment)
                await tts_segments_queue.put(None)
                for frame in _drain_tts_frames():
                    yield frame

            yield _sse_event(
                {
                    "type": "done",
                    "full_content": accumulated,
                    "assistant_turn_id": str(prep.assistant_turn_id),
                    "assistant_candidate_id": str(prep.assistant_candidate_id),
                }
            )

            chat_history_for_suggestions = (
                history_messages + [_build_user_message_with_images(outbound_user_content, list(prep.chat_images))]
            )[-app_settings.CHAT_HISTORY_LIMIT :]
            should_generate_card = (
                user_settings.reply_card_enabled or user_settings.display_mode == "detailed"
            )
            if should_generate_card:
                yield _sse_event(
                    {
                        "type": "reply_card_started",
                        "assistant_candidate_id": str(prep.assistant_candidate_id),
                    }
                )

            post_done_tasks = start_post_done_tail_tasks(
                assistant_candidate_id=str(prep.assistant_candidate_id),
                suggestions_coro=self._chat_learning_service.generate_reply_suggestions(
                    chat_history=chat_history_for_suggestions,
                    assistant_latest_message=accumulated,
                    preferred_expressions=preferred_expressions,
                    route=prep.resolved_llm_route,
                    trace_context=workflow_trace_context,
                    user_id=str(current_user.id),
                    chat_id=str(prep.chat_id),
                    turn_id=str(prep.assistant_turn_id),
                    candidate_id=str(prep.assistant_candidate_id),
                    character_id=str(prep.character.id),
                ),
                logger=logger,
                suggestions_error_log_message="User-edit reply suggestions generation failed",
                reply_card_core_coro=(
                    self._chat_learning_service.generate_reply_card(
                        assistant_text=accumulated,
                        trace_context=workflow_trace_context,
                        user_id=str(current_user.id),
                        chat_id=str(prep.chat_id),
                        turn_id=str(prep.assistant_turn_id),
                        candidate_id=str(prep.assistant_candidate_id),
                        character_id=str(prep.character.id),
                    )
                    if should_generate_card
                    else None
                ),
                reply_card_error_log_message="User-edit reply card generation failed",
            )

            if memory_active:
                # Fire-and-forget memory management for the edited user message + new assistant reply.
                spawn_bg_task(
                    self._memory_repo.manage(
                        user_text=outbound_user_content,
                        assistant_text=accumulated,
                        user_id=str(current_user.id),
                        character_id=str(prep.character.id),
                        chat_id=str(prep.chat_id),
                    ),
                    name="memory.manage_async",
                    ctx={
                        "user_id": str(current_user.id),
                        "character_id": str(prep.character.id),
                        "chat_id": str(prep.chat_id),
                    },
                )
                episodic_for_prompt = relevant_memories.get("episodic", [])[:5]
                spawn_bg_task(
                    self._memory_repo.judge_and_assign_narrative_group(
                        episodic_memories=episodic_for_prompt,
                        last_user=outbound_user_content,
                        last_assistant=accumulated,
                        user_id=str(current_user.id),
                        character_id=str(prep.character.id),
                        chat_id=str(prep.chat_id),
                    ),
                    name="memory.judge_and_assign_narrative_group_async",
                    ctx={
                        "user_id": str(current_user.id),
                        "character_id": str(prep.character.id),
                        "chat_id": str(prep.chat_id),
                    },
                )

            if tts_enabled and tts_worker_task is not None:
                deadline = time.monotonic() + 60.0
                while not tts_worker_done:
                    for frame in _drain_tts_frames():
                        yield frame
                    for frame in await _drain_post_done_frames():
                        yield frame

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        tts_error_emitted = True
                        yield _sse_event(
                            {
                                "type": "tts_error",
                                "code": "external_timeout",
                                "message": "upstream timeout: tts synthesis timed out",
                            }
                        )
                        break

                    if post_done_tasks is not None and post_done_tasks.pending:
                        next_item = await post_done_tasks.wait_for_item(min(0.05, remaining))
                        if next_item is not None:
                            for frame in await self._render_post_done_item(
                                item=next_item,
                                current_user=current_user,
                                assistant_candidate_id=prep.assistant_candidate_id,
                                persistence_log_message="User-edit reply card persistence failed",
                            ):
                                yield frame
                            continue

                    next_frame = await _wait_tts_frame(min(0.05, remaining))
                    if next_frame:
                        yield next_frame
                    elif tts_worker_task.done():
                        for frame in _drain_tts_frames():
                            yield frame
                        if not tts_worker_done:
                            tts_worker_done = True

                if tts_worker_task.done():
                    try:
                        await tts_worker_task
                    except Exception:
                        if not tts_error_emitted:
                            tts_error_emitted = True
                            yield _sse_event(
                                {
                                    "type": "tts_error",
                                    "code": "tts_upstream_error",
                                    "message": "upstream error: tts synthesis failed",
                                }
                            )

                if tts_worker_done and not tts_error_emitted:
                    yield _sse_event(
                        {
                            "type": "tts_audio_done",
                            "assistant_candidate_id": str(prep.assistant_candidate_id),
                        }
                    )

            if post_done_tasks is not None:
                while post_done_tasks.pending:
                    for frame in await _drain_post_done_frames():
                        yield frame
                    if not post_done_tasks.pending:
                        break
                    next_item = await post_done_tasks.wait_for_item(0.05)
                    if next_item is None:
                        continue
                    for frame in await self._render_post_done_item(
                        item=next_item,
                        current_user=current_user,
                        assistant_candidate_id=prep.assistant_candidate_id,
                        persistence_log_message="User-edit reply card persistence failed",
                    ):
                        yield frame

            update_observation_safe(
                workflow_observation,
                output={
                    "status": "completed",
                    "assistant_candidate_id": str(prep.assistant_candidate_id),
                    "content_length": len(accumulated),
                    "memory_active": memory_active,
                    "tts_enabled": tts_enabled,
                },
            )

        except LLMFirstChunkTimeoutError as e:
            message = (
                "upstream timeout: llm stream did not produce the first chunk in time; "
                f"model={e.model}; timeout_s={e.timeout_s:.1f}"
            )
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "llm_stream_first_chunk_timeout"},
            )
            await self._rollback_after_stream_exception(reason="llm_stream_first_chunk_timeout")
            await self._finalize_user_edit_error(
                prep=prep,
                code="llm_stream_first_chunk_timeout",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event(
                {
                    "type": "error",
                    "code": "llm_stream_first_chunk_timeout",
                    "message": message,
                }
            )
        except LLMAuthenticationError as e:
            message = e.public_message()
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "llm_service_error"},
            )
            await self._rollback_after_stream_exception(reason="llm_service_error")
            await self._finalize_user_edit_error(
                prep=prep,
                code="llm_service_error",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "llm_service_error", "message": message})
        except LLMCallError as e:
            message = (
                "upstream error: llm request failed after retries; "
                f"model={e.model}; attempts={e.attempts}"
            )
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "llm_service_error"},
            )
            await self._rollback_after_stream_exception(reason="llm_service_error")
            await self._finalize_user_edit_error(
                prep=prep,
                code="llm_service_error",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "llm_service_error", "message": message})
        except Exception as e:
            logger.exception("User edit stream error for user %s: %s", current_user.id, e)
            message = "user edit stream failed: unexpected streaming error"
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={"status": "error", "code": "user_edit_stream_failed"},
            )
            await self._rollback_after_stream_exception(reason="user_edit_stream_failed")
            await self._finalize_user_edit_error(
                prep=prep,
                code="user_edit_stream_failed",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event(
                {"type": "error", "code": "user_edit_stream_failed", "message": message}
            )
        finally:
            if post_done_tasks is not None:
                await post_done_tasks.aclose()
            if tts_worker_task and not tts_worker_task.done():
                tts_worker_task.cancel()
            workflow_context.__exit__(*sys.exc_info())

    async def _upsert_candidate_extra(
        self,
        *,
        candidate_id: uuid.UUID,
        payload: dict[str, Any],
        commit: bool = False,
    ) -> None:
        cand = await self._candidate_repo.get_by_id(candidate_id)
        if not cand:
            return
        extra = dict(cand.extra or {})
        extra.update(payload)
        cand.extra = extra
        if commit:
            await self._uow.flush()
            await self._uow.commit()

    async def _finalize_user_edit_success(
        self, prep: _UserEditPrep, full_content: str, model_type: str
    ) -> None:
        now = _now_utc()
        try:
            cand = await self._candidate_repo.get_by_id(prep.assistant_candidate_id)
            turn = await self._turn_repo.get_by_id(prep.assistant_turn_id)
            chat = await self._chat_repo.get_by_id(prep.chat_id)
            character = await self._character_repo.get_by_id(prep.character.id)
            if not cand:
                raise RuntimeError(
                    f"user edit finalize failed: candidate not found; candidate_id={prep.assistant_candidate_id}"
                )
            if not turn:
                raise RuntimeError(
                    f"user edit finalize failed: turn not found; turn_id={prep.assistant_turn_id}"
                )
            if not chat:
                raise RuntimeError(
                    f"user edit finalize failed: chat not found; chat_id={prep.chat_id}"
                )
            if not character:
                raise RuntimeError(
                    f"user edit finalize failed: character not found; character_id={prep.character.id}"
                )

            cand.content = full_content
            cand.is_final = True
            cand.model_type = model_type

            turn.state = TurnState.OK.value

            chat.last_turn_id = turn.id
            chat.active_leaf_turn_id = turn.id
            chat.last_turn_at = now

            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise

    async def _finalize_user_edit_error(
        self,
        prep: _UserEditPrep,
        code: str,
        message: str,
        partial_content: str,
    ) -> None:
        now = _now_utc()
        try:
            cand = await self._candidate_repo.get_by_id(prep.assistant_candidate_id)
            turn = await self._turn_repo.get_by_id(prep.assistant_turn_id)
            chat = await self._chat_repo.get_by_id(prep.chat_id)

            if cand:
                cand.content = partial_content or ""
                cand.is_final = True
                extra = dict(cand.extra or {})
                extra.update({"error_code": code, "error_message": message})
                cand.extra = extra

            if turn:
                turn.state = TurnState.ERROR.value

            if chat:
                chat.last_turn_id = prep.assistant_turn_id
                chat.active_leaf_turn_id = prep.assistant_turn_id
                chat.last_turn_at = now

            await self._uow.commit()
        except Exception:
            await self._uow.rollback()
            raise

    async def _rollback_after_stream_exception(self, *, reason: str) -> None:
        try:
            await self._uow.rollback()
        except Exception:
            logger.exception("Rollback failed after stream exception; reason=%s", reason)
