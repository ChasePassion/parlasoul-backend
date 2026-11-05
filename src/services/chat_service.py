"""Chat service: chat instances, turns pagination, and streaming generation.

API routes should stay thin and delegate business logic here.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
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
    CharacterStatusType,
    Chat,
    ChatOriginType,
    ChatState,
    ChatType,
    Turn,
    TurnState,
    User,
    VisibilityType,
)
from src.db.session import AsyncSessionLocal
from src.memory_system import MemoryRecord
from src.memory_system.exceptions import LLMAuthenticationError, LLMCallError
from src.observability import (
    LLMFirstChunkTimeoutError,
    create_task_in_observability_context,
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
from src.schemas.character import CharacterStatusEnum
from src.schemas.chat import (
    CandidateResponse,
    CharacterBrief,
    ChatCreateResponse,
    ChatDetailResponse,
    ChatHistoryItemResponse,
    ChatImageRef,
    ChatResponse,
    ChatsPageResponse,
    TurnResponse,
    TurnsPageResponse,
)
from src.schemas.learning import CandidateExtra
from src.schemas.media import MediaUploadKind
from src.services.bg_tasks import spawn_bg_task
from src.services.character_access_service import assert_character_chat_writable
from src.services.character_llm_route_service import ResolvedCharacterLLMRoute
from src.services.character_llm_service import CharacterLLMService
from src.services.chat_generation_service import ChatGenerationContext, build_generation_messages
from src.services.chat_learning_service import ChatLearningService
from src.services.chat_title_service import ChatTitleService
from src.services.growth_service import GrowthService
from src.services.media_url_service import (
    build_avatar_urls,
    build_chat_image_ref,
    build_r2_public_url,
    extract_chat_image_key_from_url,
    original_object_key,
    validate_avatar_image_key_owner,
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
_CHAT_TITLE_DEFAULT = app_settings.CHAT_TITLE_DEFAULT


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _encode_chat_cursor(*, sort_at: datetime, chat_id: uuid.UUID) -> str:
    payload = {
        "sort_at": sort_at.isoformat(),
        "chat_id": str(chat_id),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def _decode_chat_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        sort_at_raw = payload["sort_at"]
        chat_id_raw = payload["chat_id"]
        sort_at = datetime.fromisoformat(sort_at_raw)
        return sort_at, uuid.UUID(chat_id_raw)
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, binascii.Error) as exc:
        raise APIError(
            status_code=400,
            code="invalid_param",
            message="invalid request: cursor is invalid",
        ) from exc


def _chat_sort_at(chat: Chat) -> datetime:
    return chat.last_turn_at or chat.created_at or _now_utc()


def _derive_proactive_chat_title(content: str) -> str:
    return _CHAT_TITLE_DEFAULT


def _character_brief(
    c: Character,
    *,
    creator_username: str | None = None,
    has_unread_proactive: bool = False,
) -> CharacterBrief:
    status = getattr(c, "status", CharacterStatusType.ACTIVE.value)
    return CharacterBrief(
        id=str(c.id),
        name=c.name,
        description=c.description,
        greeting_message=c.greeting_message,
        avatar_image_key=c.avatar_image_key,
        avatar_urls=build_avatar_urls(c.avatar_image_key),
        status=CharacterStatusEnum(status),
        unpublished_at=getattr(c, "unpublished_at", None),
        visibility=c.visibility,
        creator_id=str(c.creator_id) if c.creator_id else None,
        creator_username=creator_username,
        has_unread_proactive=has_unread_proactive,
    )


def _chat_response(chat: Chat) -> ChatResponse:
    created_at = chat.created_at or _now_utc()
    updated_at = chat.updated_at or created_at
    return ChatResponse(
        id=str(chat.id),
        user_id=str(chat.user_id),
        character_id=str(chat.character_id),
        type=chat.type,
        state=chat.state,
        visibility=chat.visibility,
        origin=chat.origin,
        title=chat.title,
        last_turn_at=chat.last_turn_at,
        last_turn_id=str(chat.last_turn_id) if chat.last_turn_id else None,
        last_turn_no=int(chat.last_turn_no) if chat.last_turn_no is not None else None,
        active_leaf_turn_id=str(chat.active_leaf_turn_id) if chat.active_leaf_turn_id else None,
        last_read_turn_no=chat.last_read_turn_no,
        created_at=created_at,
        updated_at=updated_at,
        archived_at=chat.archived_at,
        meta=chat.meta,
    )


def _sanitize_candidate_extra(
    extra: dict[str, Any] | None,
    *,
    include_learning_data: bool,
) -> dict[str, Any] | None:
    if not extra:
        return None

    def _strip_reply_card_sentence_ipa(card: Any) -> Any:
        if not isinstance(card, dict):
            return card
        cleaned = dict(card)
        cleaned.pop("ipa_us", None)
        return cleaned

    filtered = dict(extra)
    reply_card_payload = filtered.get("reply_card")
    if reply_card_payload is not None:
        filtered["reply_card"] = _strip_reply_card_sentence_ipa(reply_card_payload)
    if include_learning_data:
        return filtered
    filtered.pop("input_transform", None)
    filtered.pop("reply_card", None)
    return filtered or None


def _candidate_response(
    cand: Candidate,
    *,
    include_learning_data: bool = True,
) -> CandidateResponse:
    extra_payload = _sanitize_candidate_extra(
        cand.extra,
        include_learning_data=include_learning_data,
    )
    chat_images = _materialize_chat_image_refs((cand.extra or {}).get("chat_images"))
    return CandidateResponse(
        id=str(cand.id),
        candidate_no=int(cand.candidate_no),
        content=cand.content,
        model_type=cand.model_type,
        is_final=bool(cand.is_final),
        rank=int(cand.rank) if cand.rank is not None else None,
        created_at=cand.created_at or _now_utc(),
        extra=CandidateExtra.model_validate(extra_payload) if extra_payload else None,
        chat_images=chat_images,
    )


def _materialize_chat_image_refs(raw_items: Any) -> list[ChatImageRef] | None:
    if not isinstance(raw_items, list):
        return None

    refs: list[ChatImageRef] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        image_key = item.get("image_key")
        if not isinstance(image_key, str):
            image_key = extract_chat_image_key_from_url(item.get("original")) or extract_chat_image_key_from_url(
                item.get("display")
            )
        if not isinstance(image_key, str):
            continue
        ref = build_chat_image_ref(image_key)
        if ref is None or ref.image_key in seen:
            continue
        seen.add(ref.image_key)
        refs.append(ref)
    return refs or None


def _normalize_requested_chat_images(
    images: list[ChatImageRef] | None,
    *,
    owner_id: str,
) -> tuple[ChatImageRef, ...]:
    normalized_refs: list[ChatImageRef] = []
    seen: set[str] = set()
    for image in images or []:
        normalized_key = validate_avatar_image_key_owner(
            image.image_key,
            owner_id=owner_id,
            allowed_kinds=(MediaUploadKind.CHAT_IMAGE,),
        )
        if normalized_key is None:
            logger.warning(
                "Chat image rejected: not owned by user image_key=%s owner_id=%s",
                image.image_key,
                owner_id,
            )
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: chat image key is not owned by current user",
            )
        ref = build_chat_image_ref(normalized_key)
        if ref is None:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: chat image key is invalid",
            )
        if ref.image_key in seen:
            continue
        seen.add(ref.image_key)
        normalized_refs.append(ref)
    if normalized_refs:
        logger.info(
            "Chat images normalized: count=%d keys=%s",
            len(normalized_refs),
            [r.image_key for r in normalized_refs],
        )
    return tuple(normalized_refs)


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def _message_has_image_parts(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get("type") == "image_url" for item in content
    )


def _llm_image_url(image_key: str) -> str:
    r2_url = build_r2_public_url(original_object_key(image_key))
    final_url = r2_url if r2_url else f"/media/{image_key}/original"
    logger.info(
        "Built LLM image URL.",
        extra={
            "log_module": "chat",
            "event": "chat.image.llm_url_built",
            "operation": "chat.reply.stream",
            "image_key": image_key,
            "r2_public_base": app_settings.R2_PUBLIC_BASE_URL,
            "final_url": final_url,
        },
    )
    return final_url


def _build_user_message_with_images(
    text: str,
    chat_images: list[ChatImageRef] | None,
) -> dict[str, Any]:
    """Build a user message dict, including image_url parts when chat_images are present.

    Returns {"role": "user", "content": <text or multimodal array>}.
    """
    if chat_images:
        parts: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": _llm_image_url(ref.image_key)}}
            for ref in chat_images
        ]
        parts.append({"type": "text", "text": text})
        return {"role": "user", "content": parts}
    return {"role": "user", "content": text}


async def _load_llm_history_messages_from_leaf(
    turn_repo: TurnRepository,
    candidate_repo: CandidateRepository,
    chat_id: uuid.UUID,
    leaf_turn_id: uuid.UUID | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Load history messages along a single branch ending at leaf_turn_id.

    The returned list is in chronological order and excludes non-final /
    empty candidates. Handles chat_images in user candidates.

    This is the canonical implementation shared by chat_service and
    turn_service.  When adding new message capabilities (images, audio,
    citations, etc.), update BOTH the history extraction path (here) AND
    the current-message builder (_build_user_message_with_images above).
    """
    if leaf_turn_id is None or limit <= 0:
        return []

    collected, _ = await turn_repo.get_ancestor_chain(
        leaf_turn_id=leaf_turn_id,
        chat_id=chat_id,
        limit=limit,
    )

    if not collected:
        return []

    primary_ids = [
        t.primary_candidate_id for t in collected if t.primary_candidate_id is not None
    ]
    cand_map: dict[uuid.UUID, Candidate] = {}
    if primary_ids:
        cand_rows = await candidate_repo.get_by_ids(primary_ids)
        cand_map = {c.id: c for c in cand_rows}

    messages: list[dict[str, Any]] = []
    for t in reversed(collected):
        if not t.primary_candidate_id:
            continue
        cand = cand_map.get(t.primary_candidate_id)
        if not cand:
            continue
        if not cand.is_final:
            continue
        if not (cand.content or "").strip():
            continue

        if t.author_type == AuthorType.USER.value:
            chat_image_refs = _materialize_chat_image_refs((cand.extra or {}).get("chat_images"))
            if chat_image_refs:
                parts: list[dict[str, Any]] = [
                    {"type": "image_url", "image_url": {"url": _llm_image_url(ref.image_key)}}
                    for ref in chat_image_refs
                ]
                parts.append({"type": "text", "text": cand.content})
                messages.append({"role": "user", "content": parts})
            else:
                messages.append({"role": "user", "content": cand.content})
        elif t.author_type == AuthorType.CHARACTER.value:
            messages.append({"role": "assistant", "content": cand.content})
        else:
            # SYSTEM turns are not part of user-visible history.
            continue

    return messages


def _text_only_chat_history(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = _message_content_to_text(message.get("content"))
        if not text:
            continue
        normalized.append({"role": role, "content": text})
    return normalized


def _turn_response(
    turn: Turn,
    primary: Candidate,
    candidate_count: int,
    *,
    include_learning_data: bool = True,
) -> TurnResponse:
    source = None
    if isinstance(primary.extra, dict):
        raw_source = primary.extra.get("source")
        if isinstance(raw_source, str) and raw_source.strip():
            source = raw_source
    return TurnResponse(
        id=str(turn.id),
        turn_no=int(turn.turn_no),
        author_type=turn.author_type,
        state=turn.state,
        is_proactive=bool(turn.is_proactive),
        source=source,
        parent_turn_id=str(turn.parent_turn_id) if turn.parent_turn_id else None,
        parent_candidate_id=str(turn.parent_candidate_id) if turn.parent_candidate_id else None,
        primary_candidate=_candidate_response(primary, include_learning_data=include_learning_data),
        candidate_count=int(candidate_count),
    )


def _assert_character_access(character: Character, user: User) -> None:
    if character.visibility in (VisibilityType.PUBLIC.value, VisibilityType.PRIVATE.value):
        return

    is_creator = character.creator_id is not None and character.creator_id == user.id
    if not is_creator:
        raise APIError(
            status_code=403,
            code="character_private_forbidden",
            message=(
                "authorization failed: private character is not accessible; "
                f"character_id={character.id}"
            ),
        )




# ---------------------------------------------------------------------------
# System prompt construction lives in the shared generation seam
# (src/services/chat_generation_service.py); chat, turn and realtime paths all
# build inputs through it. The orchestration functions below
# (_upsert_candidate_extra, _finalize_*_success/error, TTS worker closure)
# have near-identical counterparts in turn_service.py.
#
# _load_llm_history_messages_from_leaf and _build_user_message_with_images are
# shared via the module-level functions above — do NOT duplicate them elsewhere.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _StreamPrep:
    chat: Chat
    chat_id: uuid.UUID
    character: Character
    character_id: uuid.UUID
    resolved_llm_route: ResolvedCharacterLLMRoute
    history_leaf_turn_id: uuid.UUID | None
    should_generate_title: bool
    user_turn_id: uuid.UUID
    user_turn_no: int
    user_candidate_id: uuid.UUID
    assistant_turn_id: uuid.UUID
    assistant_turn_no: int
    assistant_candidate_id: uuid.UUID
    timing: dict[str, float] | None = None
    has_images: bool = False
    chat_images: tuple[ChatImageRef, ...] = ()


class ChatService:
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
        chat_title_service: ChatTitleService | None = None,
        growth_service: GrowthService | None = None,
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
        self._chat_title_service = chat_title_service or ChatTitleService()
        self._growth_service = growth_service
        self._voice_service = voice_service
        self._character_llm_service = character_llm_service or CharacterLLMService()
        self._subscription_service = subscription_service

    async def prepare_stream(
        self,
        current_user: User,
        chat_id: uuid.UUID,
        content: str,
        images: list[ChatImageRef] | None = None,
    ) -> _StreamPrep:
        """Run all validations and create placeholder turns before SSE starts.

        This must be executed before returning StreamingResponse so API errors
        can be returned as normal JSON error envelopes (non-200).
        """
        return await self._prepare_stream(
            current_user=current_user, chat_id=chat_id, content=content, images=images
        )

    async def get_recent_chat_detail(
        self, current_user: User, character_id: uuid.UUID
    ) -> ChatDetailResponse | None:
        character = await self._character_repo.get_by_id(character_id)
        if not character:
            raise APIError(
                status_code=404,
                code="character_not_found",
                message=f"resource not found: character does not exist; character_id={character_id}",
            )
        if (
            getattr(character, "status", CharacterStatusType.ACTIVE.value)
            == CharacterStatusType.ACTIVE.value
        ):
            _assert_character_access(character, current_user)

            async with self._uow.transactional():
                await self._chat_repo.delete_empty_drafts(
                    user_id=current_user.id,
                    character_id=character.id,
                    default_title=_CHAT_TITLE_DEFAULT,
                )
                chat = await self._chat_repo.get_recent_active(
                    user_id=current_user.id,
                    character_id=character.id,
                )
        else:
            chat = await self._chat_repo.get_recent_active(
                user_id=current_user.id,
                character_id=character.id,
            )
        if not chat:
            return None

        if (
            chat.origin == ChatOriginType.PROACTIVE.value
            and (chat.last_read_turn_no is None or chat.last_read_turn_no < chat.last_turn_no)
        ):
            async with self._uow.transactional():
                await self._chat_repo.mark_proactive_chat_read(chat)

        return ChatDetailResponse(chat=_chat_response(chat), character=_character_brief(character))

    async def list_chats_page(
        self,
        *,
        current_user: User,
        character_id: uuid.UUID,
        cursor: str | None,
        limit: int,
    ) -> ChatsPageResponse:
        character = await self._character_repo.get_by_id(character_id)
        if not character:
            raise APIError(
                status_code=404,
                code="character_not_found",
                message=f"resource not found: character does not exist; character_id={character_id}",
            )
        if (
            getattr(character, "status", CharacterStatusType.ACTIVE.value)
            == CharacterStatusType.ACTIVE.value
        ):
            _assert_character_access(character, current_user)

        cursor_sort_at: datetime | None = None
        cursor_chat_id: uuid.UUID | None = None
        if cursor:
            cursor_sort_at, cursor_chat_id = _decode_chat_cursor(cursor)

        rows, has_more = await self._chat_repo.list_active_by_user_and_character(
            user_id=current_user.id,
            character_id=character.id,
            cursor_sort_at=cursor_sort_at,
            cursor_chat_id=cursor_chat_id,
            limit=limit,
        )

        items = [
            ChatHistoryItemResponse(
                chat=_chat_response(chat),
                character=_character_brief(chat_character),
            )
            for chat, chat_character in rows
        ]
        next_cursor = None
        if has_more and rows:
            last_chat, _ = rows[-1]
            next_cursor = _encode_chat_cursor(
                sort_at=_chat_sort_at(last_chat),
                chat_id=last_chat.id,
            )

        return ChatsPageResponse(
            items=items,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def update_chat_title(
        self,
        *,
        current_user: User,
        chat_id: uuid.UUID,
        title: str | None,
    ) -> ChatResponse:
        if title is None:
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: title cannot be null",
            )
        normalized_title = title.strip()
        if not normalized_title:
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: title cannot be empty",
            )

        chat = await self._chat_repo.get_by_id(chat_id)
        if not chat:
            raise APIError(
                status_code=404,
                code="chat_not_found",
                message=f"resource not found: chat does not exist; chat_id={chat_id}",
            )
        if chat.user_id != current_user.id:
            raise APIError(
                status_code=403,
                code="chat_forbidden",
                message=f"authorization failed: chat is not accessible; chat_id={chat_id}",
            )

        async with self._uow.transactional():
            chat.title = normalized_title
            await self._uow.flush()

        await self._uow.refresh(chat)
        return _chat_response(chat)

    async def delete_chat(
        self,
        *,
        current_user: User,
        chat_id: uuid.UUID,
    ) -> None:
        chat = await self._chat_repo.get_by_id(chat_id)
        if not chat:
            raise APIError(
                status_code=404,
                code="chat_not_found",
                message=f"resource not found: chat does not exist; chat_id={chat_id}",
            )
        if chat.user_id != current_user.id:
            raise APIError(
                status_code=403,
                code="chat_forbidden",
                message=f"authorization failed: chat is not accessible; chat_id={chat_id}",
            )

        async with self._uow.transactional():
            await self._chat_repo.delete(chat)

    async def create_chat(
        self, current_user: User, character_id: uuid.UUID, meta: dict[str, Any] | None
    ) -> ChatCreateResponse:
        initial_turns: list[TurnResponse] = []
        now = _now_utc()

        async with self._uow.transactional():
            character = await self._character_repo.get_by_id(character_id)
            if not character:
                raise APIError(
                    status_code=404,
                    code="character_not_found",
                    message=f"resource not found: character does not exist; character_id={character_id}",
                )
            _assert_character_access(character, current_user)
            assert_character_chat_writable(character, user_id=current_user.id)

            chat = Chat(
                user_id=current_user.id,
                character_id=character.id,
                type=ChatType.ONE_ON_ONE.value,
                state=ChatState.ACTIVE.value,
                visibility=VisibilityType.PRIVATE.value,
                origin=ChatOriginType.USER.value,
                title=_CHAT_TITLE_DEFAULT,
                meta=meta,
                last_turn_no=0,
            )
            self._chat_repo.add(chat)
            await self._uow.flush()

            greeting = (character.greeting_message or "").strip()
            if greeting:
                turn = Turn(
                    chat_id=chat.id,
                    turn_no=1,
                    author_type=AuthorType.CHARACTER.value,
                    author_character_id=character.id,
                    state=TurnState.OK.value,
                    is_proactive=True,
                )
                self._turn_repo.add(turn)
                await self._uow.flush()

                cand = Candidate(
                    turn_id=turn.id,
                    candidate_no=1,
                    content=greeting,
                    is_final=True,
                    extra={"source": "greeting"},
                )
                self._candidate_repo.add(cand)
                await self._uow.flush()
                # created_at is server_default; refresh to avoid None/expired access in async context
                await self._uow.refresh(cand)

                turn.primary_candidate_id = cand.id
                chat.last_turn_id = turn.id
                chat.last_turn_at = now
                chat.last_turn_no = turn.turn_no
                chat.active_leaf_turn_id = turn.id

                initial_turns.append(_turn_response(turn, cand, candidate_count=1))
                if self._growth_service is not None:
                    await self._growth_service.record_greeting_inserted(
                        user_id=current_user.id,
                        character_id=character.id,
                        chat_id=chat.id,
                        greeting_text=greeting,
                        occurred_at=now,
                    )

        # Refresh to ensure timestamps are populated
        await self._uow.refresh(chat)

        return ChatCreateResponse(
            chat=_chat_response(chat),
            character=_character_brief(character),
            initial_turns=initial_turns,
        )

    async def create_proactive_chat(
        self,
        *,
        current_user: User,
        character: Character,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> tuple[Chat, Turn, Candidate]:
        message = content.strip()
        if not message:
            raise APIError(
                status_code=400,
                code="invalid_param",
                message="invalid request: proactive message content cannot be empty",
            )

        assert_character_chat_writable(character, user_id=current_user.id)
        now = _now_utc()

        async with self._uow.transactional():
            chat = Chat(
                user_id=current_user.id,
                character_id=character.id,
                type=ChatType.ONE_ON_ONE.value,
                state=ChatState.ACTIVE.value,
                visibility=VisibilityType.PRIVATE.value,
                origin=ChatOriginType.PROACTIVE.value,
                title=_derive_proactive_chat_title(message),
                meta=meta,
                last_turn_no=0,
            )
            self._chat_repo.add(chat)
            await self._uow.flush()

            turn = Turn(
                chat_id=chat.id,
                turn_no=1,
                author_type=AuthorType.CHARACTER.value,
                author_character_id=character.id,
                state=TurnState.OK.value,
                is_proactive=True,
            )
            self._turn_repo.add(turn)
            await self._uow.flush()

            candidate = Candidate(
                turn_id=turn.id,
                candidate_no=1,
                content=message,
                is_final=True,
                extra={"source": "proactive_outreach"},
            )
            self._candidate_repo.add(candidate)
            await self._uow.flush()
            await self._uow.refresh(candidate)

            turn.primary_candidate_id = candidate.id
            chat.last_turn_id = turn.id
            chat.last_turn_at = now
            chat.last_turn_no = turn.turn_no
            chat.active_leaf_turn_id = turn.id

            await self._chat_repo.archive_stale_unreplied_proactive_chats(
                user_id=current_user.id,
                character_id=character.id,
                keep_chat_id=chat.id,
                archived_at=now,
            )

        await self._uow.refresh(chat)
        return chat, turn, candidate

    async def get_turns_page(
        self,
        current_user: User,
        chat_id: uuid.UUID,
        before_turn_id: uuid.UUID | None,
        limit: int,
        include_learning_data: bool = True,
    ) -> TurnsPageResponse:
        chat = await self._chat_repo.get_by_id(chat_id)
        if not chat:
            raise APIError(
                status_code=404,
                code="chat_not_found",
                message=f"resource not found: chat does not exist; chat_id={chat_id}",
            )

        if chat.user_id != current_user.id:
            raise APIError(
                status_code=403,
                code="chat_forbidden",
                message=f"authorization failed: chat is not accessible; chat_id={chat_id}",
            )

        character = await self._character_repo.get_by_id(chat.character_id)
        if not character:
            raise APIError(
                status_code=404,
                code="character_not_found",
                message=(
                    "resource not found: character does not exist; "
                    f"character_id={chat.character_id}"
                ),
            )

        if before_turn_id is None:
            await self._schedule_chat_tts_warmup(
                current_user=current_user,
                chat=chat,
                character=character,
            )

        # Cursor pagination along the currently active branch.
        start_turn_id: uuid.UUID | None
        if before_turn_id is None:
            start_turn_id = chat.active_leaf_turn_id or chat.last_turn_id
        else:
            before_turn = await self._turn_repo.get_by_id(before_turn_id)
            if not before_turn or before_turn.chat_id != chat.id:
                raise APIError(
                    status_code=404,
                    code="turn_not_found",
                    message=(
                        "resource not found: turn does not exist in chat; "
                        f"chat_id={chat.id}; turn_id={before_turn_id}"
                    ),
                )
            start_turn_id = before_turn.parent_turn_id

        collected, has_more = await self._turn_repo.get_ancestor_chain(
            leaf_turn_id=start_turn_id,
            chat_id=chat.id,
            limit=limit,
        )

        primary_ids = [
            t.primary_candidate_id for t in collected if t.primary_candidate_id is not None
        ]
        cand_map: dict[uuid.UUID, Candidate] = {}
        if primary_ids:
            cand_rows = await self._candidate_repo.get_by_ids(primary_ids)
            cand_map = {c.id: c for c in cand_rows}

        turn_ids = [t.id for t in collected]
        count_map = await self._candidate_repo.count_by_turn_ids(turn_ids)

        turns: list[TurnResponse] = []
        for t in reversed(collected):
            if not t.primary_candidate_id:
                continue
            primary = cand_map.get(t.primary_candidate_id)
            if not primary:
                continue
            turns.append(
                _turn_response(
                    t,
                    primary,
                    candidate_count=count_map.get(t.id, 1),
                    include_learning_data=include_learning_data,
                )
            )

        next_before = turns[0].id if turns else None

        return TurnsPageResponse(
            chat=_chat_response(chat),
            character=_character_brief(character),
            turns=turns,
            next_before_turn_id=next_before,
            has_more=has_more,
        )

    async def _schedule_chat_tts_warmup(
        self,
        *,
        current_user: User,
        chat: Chat,
        character: Character,
    ) -> None:
        if self._voice_service is None:
            return

        try:
            user_settings = await self._user_settings_repo.get_by_user_id(current_user.id)
        except Exception:
            logger.exception(
                "Chat TTS warm-up settings lookup failed.",
                extra={
                    "log_module": "voice",
                    "event": "chat.tts.warm_schedule_failed",
                    "operation": "chat.turns.page",
                    "chat_id": str(chat.id),
                    "character_id": str(character.id),
                },
            )
            return

        auto_read_enabled = (
            bool(user_settings.auto_read_aloud_enabled)
            if user_settings is not None
            else bool(app_settings.USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT)
        )
        if not auto_read_enabled:
            return

        provider = (getattr(character, "voice_provider", "") or "").strip()
        provider_model = (getattr(character, "voice_model", "") or "").strip() or None
        provider_voice_id = (
            getattr(character, "voice_provider_voice_id", "") or ""
        ).strip() or None
        voice_source_type = (getattr(character, "voice_source_type", "") or "").strip() or "system"
        if not provider:
            return

        spawn_bg_task(
            self._voice_service.warm_tts_sse_events_from_segments(
                provider=provider,
                provider_voice_id=provider_voice_id,
                provider_model=provider_model,
                voice_source_type=voice_source_type,
            ),
            name="chat.tts_warmup",
            ctx={
                "chat_id": str(chat.id),
                "character_id": str(character.id),
                "provider": provider,
                "provider_voice_id": provider_voice_id,
            },
        )
        logger.info(
            "Chat TTS warm-up scheduled.",
            extra={
                "log_module": "voice",
                "event": "chat.tts.warm_scheduled",
                "operation": "chat.turns.page",
                "provider": provider,
                "model": provider_model,
                "provider_voice_id": provider_voice_id,
                "audio_format": "pcm",
                "chat_id": str(chat.id),
                "character_id": str(character.id),
            },
        )

    async def stream_chat(
        self,
        current_user: User,
        chat_id: uuid.UUID,
        content: str,
    ) -> AsyncGenerator[str, None]:
        prep = await self._prepare_stream(
            current_user=current_user, chat_id=chat_id, content=content
        )
        async for item in self.stream_chat_with_prep(
            prep=prep,
            current_user=current_user,
            content=content,
        ):
            yield item

    async def stream_chat_with_prep(
        self,
        prep: _StreamPrep,
        current_user: User,
        content: str,
        images: list[ChatImageRef] | None = None,
    ) -> AsyncGenerator[str, None]:

        timing: dict[str, float] = dict(prep.timing) if prep.timing else {}
        t_stream_start = time.monotonic()

        # 1) Emit meta so frontend can anchor optimistic UI to server IDs.
        yield _sse_event(
            {
                "type": "meta",
                "user_turn": {
                    "id": str(prep.user_turn_id),
                    "turn_no": prep.user_turn_no,
                    "candidate_id": str(prep.user_candidate_id),
                },
                "assistant_turn": {
                    "id": str(prep.assistant_turn_id),
                    "turn_no": prep.assistant_turn_no,
                    "candidate_id": str(prep.assistant_candidate_id),
                },
            }
        )

        accumulated = ""
        outbound_user_content = content
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
        tts_first_segment_queued_at: float | None = None
        tts_first_audio_emitted = False
        tts_queued_segment_count = 0
        post_done_tasks: PostDoneTailTasks | None = None
        title_task: asyncio.Task[str | None] | None = None
        title_event_emitted = False
        workflow_context = start_workflow_observation(
            name="chat.turn.stream",
            input_payload={
                "chat_id": str(prep.chat_id),
                "user_turn_id": str(prep.user_turn_id),
                "assistant_turn_id": str(prep.assistant_turn_id),
                "assistant_candidate_id": str(prep.assistant_candidate_id),
                "content_length": len(content),
            },
            user_id=str(current_user.id),
            session_id=f"chat:{prep.chat_id}",
            tags=["chat", "turn", "stream"],
            metadata={
                "workflow": "chat.reply.stream",
                "chat_id": str(prep.chat_id),
                "character_id": str(prep.character_id),
                "assistant_candidate_id": str(prep.assistant_candidate_id),
            },
            manual=True,
        )
        workflow_observation = workflow_context.__enter__()
        workflow_trace_context = current_trace_context(workflow_observation)

        try:
            t_obs_ctx = time.monotonic()
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
            timing["stream_observability_ctx"] = time.monotonic() - t_obs_ctx

            t_user_settings = time.monotonic()
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
            timing["stream_db_user_settings"] = time.monotonic() - t_user_settings

            memory_active = await self._subscription_service.has_feature(
                current_user, "memory_feature"
            ) and bool(user_settings.memory_enabled)
            tts_enabled = bool(self._voice_service and user_settings.auto_read_aloud_enabled)
            logger.info(
                "Chat stream TTS: chat_id=%s enabled=%s voice_service=%s auto_read_aloud=%s voice_id=%s",
                prep.chat_id,
                tts_enabled,
                bool(self._voice_service),
                user_settings.auto_read_aloud_enabled,
                getattr(prep.character, "voice_provider_voice_id", ""),
            )
            if tts_enabled and prep.character.voice_provider_voice_id.strip():
                tts_provider = prep.character.voice_provider
                tts_provider_voice_id = prep.character.voice_provider_voice_id
                tts_provider_model = prep.character.voice_model
                tts_voice_source_type = prep.character.voice_source_type

            def _consume_tts_event(event: dict[str, Any]) -> str | None:
                nonlocal tts_worker_done
                nonlocal tts_error_emitted
                nonlocal tts_first_audio_emitted
                event_type = event.get("type")
                if event_type == "tts_worker_done":
                    tts_worker_done = True
                    return None
                if event_type == "tts_error":
                    tts_error_emitted = True
                if event_type == "tts_audio_delta" and not tts_first_audio_emitted:
                    tts_first_audio_emitted = True
                    now = time.monotonic()
                    logger.info(
                        "Chat stream first TTS audio emitted.",
                        extra={
                            "log_module": "voice",
                            "event": "chat.tts.first_audio_emitted",
                            "operation": "chat.reply.stream",
                            "chat_id": str(prep.chat_id),
                            "turn_id": str(prep.assistant_turn_id),
                            "candidate_id": str(prep.assistant_candidate_id),
                            "character_id": str(prep.character_id),
                            "elapsed_ms": int((now - t_stream_start) * 1000),
                            "first_segment_to_audio_ms": (
                                int((now - tts_first_segment_queued_at) * 1000)
                                if tts_first_segment_queued_at is not None
                                else None
                            ),
                            "queued_segments": tts_queued_segment_count,
                            "seq": event.get("seq"),
                            "mime_type": event.get("mime_type"),
                        },
                    )
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

            async def _queue_tts_segment(segment: str, *, source: str) -> None:
                nonlocal tts_first_segment_queued_at
                nonlocal tts_queued_segment_count
                if tts_segments_queue is None:
                    return
                text = segment.strip()
                if not text:
                    return

                now = time.monotonic()
                tts_queued_segment_count += 1
                if tts_first_segment_queued_at is None:
                    tts_first_segment_queued_at = now
                    logger.info(
                        "Chat stream first TTS segment queued.",
                        extra={
                            "log_module": "voice",
                            "event": "chat.tts.first_segment_queued",
                            "operation": "chat.reply.stream",
                            "chat_id": str(prep.chat_id),
                            "turn_id": str(prep.assistant_turn_id),
                            "candidate_id": str(prep.assistant_candidate_id),
                            "character_id": str(prep.character_id),
                            "elapsed_ms": int((now - t_stream_start) * 1000),
                            "source": source,
                            "segment_chars": len(text),
                            "accumulated_chars": len(accumulated),
                        },
                    )
                await tts_segments_queue.put(text)

            async def _render_post_done_item(
                item: dict[str, Any] | ReplyCardCoreReady,
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
                        candidate_id=prep.assistant_candidate_id,
                        payload={"reply_card": reply_card.model_dump()},
                        commit=True,
                    )
                except Exception:
                    logger.exception("Reply card persistence failed")
                    return [
                        _sse_event(
                            {
                                "type": "reply_card_error",
                                "assistant_candidate_id": str(prep.assistant_candidate_id),
                                "code": "reply_card_persist_failed",
                                "message": "internal error: reply card persistence failed",
                            }
                        )
                    ]

                return [
                    _sse_event(
                        {
                            "type": "reply_card",
                            "assistant_candidate_id": str(prep.assistant_candidate_id),
                            "reply_card": reply_card.model_dump(),
                        }
                    )
                ]

            async def _drain_post_done_frames() -> list[str]:
                if post_done_tasks is None:
                    return []

                frames: list[str] = []
                for item in post_done_tasks.drain_nowait():
                    frames.extend(await _render_post_done_item(item))
                return frames

            def _drain_title_frames() -> list[str]:
                nonlocal title_event_emitted
                if title_task is None or not title_task.done() or title_event_emitted:
                    return []

                title_event_emitted = True
                try:
                    title = title_task.result()
                except Exception:
                    logger.exception("Chat title generation failed; chat_id=%s", prep.chat_id)
                    return []

                if not title:
                    return []

                return [
                    _sse_event(
                        {
                            "type": "chat_title_updated",
                            "chat_id": str(prep.chat_id),
                            "title": title,
                        }
                    )
                ]

            if prep.should_generate_title:
                title_task = create_task_in_observability_context(
                    self._generate_and_persist_chat_title(
                        current_user_id=current_user.id,
                        chat_id=prep.chat_id,
                        first_user_message=content,
                        trace_context=workflow_trace_context,
                    ),
                    name="chat.auto_title",
                )

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
                        logger.exception("TTS worker failed: %s", exc)
                        await tts_events_queue.put(
                            {
                                "type": "tts_error",
                                "code": "tts_upstream_error",
                                "message": (
                                    "upstream error: tts synthesis failed during chat stream"
                                ),
                            }
                        )
                    finally:
                        await tts_events_queue.put({"type": "tts_worker_done"})

                tts_worker_task = asyncio.create_task(
                    _tts_worker(),
                    name="chat.tts_worker",
                )

            if (
                user_settings.mixed_input_auto_translate_enabled
                and self._chat_learning_service.contains_chinese(content)
            ):
                t_translate = time.monotonic()
                translated = content
                try:
                    translated = await self._chat_learning_service.translate_mixed_to_english(
                        content,
                        trace_context=workflow_trace_context,
                        user_id=str(current_user.id),
                        chat_id=str(prep.chat_id),
                        turn_id=str(prep.user_turn_id),
                        character_id=str(prep.character_id),
                    )
                except Exception:
                    logger.exception("Mixed-input translation failed; fallback to original content")
                    translated = content
                timing["stream_translate"] = time.monotonic() - t_translate

                outbound_user_content = translated or content
                transform_payload = {
                    "applied": bool(
                        outbound_user_content.strip() and outbound_user_content != content
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
                        "original_content": content,
                        "transformed_content": outbound_user_content,
                        "applied": transform_payload["applied"],
                    }
                )

            # 2) Load history from DB (do not trust client-provided history).
            t_history = time.monotonic()
            history_messages = await self._load_llm_history_messages_from_leaf(
                chat_id=prep.chat_id,
                leaf_turn_id=prep.history_leaf_turn_id,
                limit=app_settings.CHAT_HISTORY_LIMIT,
            )
            timing["stream_db_history"] = time.monotonic() - t_history

            # 3) Retrieve long-term memories (scope: user_id + character_id).
            relevant_memories: dict[str, list[MemoryRecord]] = {"episodic": [], "semantic": []}
            if memory_active:
                t_memory = time.monotonic()
                relevant_memories = await safe_memory_search(
                    self._memory_repo,
                    query=outbound_user_content,
                    user_id=str(current_user.id),
                    character_id=str(prep.character_id),
                    chat_id=str(prep.chat_id),
                    timeout_s=self._memory_repo.allowed_memory_search_timeout_s,
                )
                timing["stream_memory_search"] = time.monotonic() - t_memory
            preferred_expressions = []
            if user_settings.preferred_expression_bias_enabled:
                t_pref = time.monotonic()
                preferred_expressions = (
                    await self._chat_learning_service.load_preferred_expressions(
                        user_id=current_user.id,
                    )
                )
                timing["stream_preferred_expressions"] = time.monotonic() - t_pref

            t_prompt = time.monotonic()
            generation_context = ChatGenerationContext(
                character_system_prompt=prep.character.system_prompt,
                dialogue_style_id=getattr(prep.character, "dialogue_style_id", None),
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
            timing["stream_build_prompt"] = time.monotonic() - t_prompt

            # 4) Stream model output.
            t_llm_start = time.monotonic()
            first_chunk_received = False
            has_multimodal_context = bool(prep.chat_images) or any(
                _message_has_image_parts(message) for message in history_messages
            )
            active_route = (
                self._character_llm_service.resolve_vision_route()
                if has_multimodal_context
                else prep.resolved_llm_route
            )
            async for chunk in self._character_llm_service.chat_stream_messages(
                messages=messages,
                route=active_route,
                operation="chat.reply.stream",
                route_role="chat",
                trace_context=workflow_trace_context,
                user_id=str(current_user.id),
                chat_id=str(prep.chat_id),
                turn_id=str(prep.assistant_turn_id),
                candidate_id=str(prep.assistant_candidate_id),
                character_id=str(prep.character_id),
            ):
                if not first_chunk_received:
                    first_chunk_received = True
                    timing["stream_llm_ttft"] = time.monotonic() - t_llm_start
                    timing["stream_total_ttft"] = time.monotonic() - t_stream_start
                    timing["total_e2e_ttft"] = timing["stream_total_ttft"] + timing.get(
                        "prep_total", 0
                    )
                    # Round all values to ms
                    timing_ms = {k: round(v * 1000, 1) for k, v in timing.items()}
                    logger.info(
                        "TTFT profile chat_id=%s model=%s: %s",
                        prep.chat_id,
                        active_route.model,
                        json.dumps(timing_ms),
                    )
                    yield _sse_event({"type": "ttft_profile", "timing_ms": timing_ms})
                accumulated += chunk
                yield _sse_event({"type": "chunk", "content": chunk})
                if tts_enabled and tts_segments_queue is not None and tts_segmenter is not None:
                    for segment in tts_segmenter.feed(chunk):
                        await _queue_tts_segment(segment, source="feed")
                for frame in _drain_tts_frames():
                    yield frame
                for frame in _drain_title_frames():
                    yield frame

            # 5) Persist final assistant candidate + update chat pointers.
            if tts_enabled and tts_segmenter is not None:
                logger.info(
                    "Chat stream LLM completed before TTS tail flush.",
                    extra={
                        "log_module": "voice",
                        "event": "chat.tts.llm_completed",
                        "operation": "chat.reply.stream",
                        "chat_id": str(prep.chat_id),
                        "turn_id": str(prep.assistant_turn_id),
                        "candidate_id": str(prep.assistant_candidate_id),
                        "character_id": str(prep.character_id),
                        "elapsed_ms": int((time.monotonic() - t_stream_start) * 1000),
                        "accumulated_chars": len(accumulated),
                        "buffer_chars": len(tts_segmenter.buffer.strip()),
                        "queued_segments": tts_queued_segment_count,
                        "first_segment_queued": tts_first_segment_queued_at is not None,
                    },
                )

            t_finalize_start = time.monotonic()
            growth_update = await self._finalize_stream_success(
                prep=prep,
                current_user_id=current_user.id,
                user_message=content,
                full_content=accumulated,
                model_type=active_route.model_type_label,
            )
            t_finalize_end = time.monotonic()

            if tts_enabled and tts_segments_queue is not None and tts_segmenter is not None:
                tail_segments = tts_segmenter.flush()
                logger.info(
                    "Chat stream TTS tail flush queued.",
                    extra={
                        "log_module": "voice",
                        "event": "chat.tts.tail_flush",
                        "operation": "chat.reply.stream",
                        "chat_id": str(prep.chat_id),
                        "turn_id": str(prep.assistant_turn_id),
                        "candidate_id": str(prep.assistant_candidate_id),
                        "character_id": str(prep.character_id),
                        "elapsed_ms": int((time.monotonic() - t_stream_start) * 1000),
                        "finalize_ms": int((t_finalize_end - t_finalize_start) * 1000),
                        "tail_segment_count": len(tail_segments),
                        "tail_chars": sum(len(segment.strip()) for segment in tail_segments),
                        "queued_segments_before_tail": tts_queued_segment_count,
                    },
                )
                for tail_segment in tail_segments:
                    await _queue_tts_segment(tail_segment, source="tail_flush")
                await tts_segments_queue.put(None)
                for frame in _drain_tts_frames():
                    yield frame
            for frame in _drain_title_frames():
                yield frame
            if growth_update is not None:
                yield _sse_event(
                    {
                        "type": "growth_daily_updated",
                        "today": growth_update.today.model_dump(mode="json"),
                    }
                )
                for share_card in growth_update.share_cards:
                    yield _sse_event(
                        {
                            "type": "growth_share_card_ready",
                            "share_card": share_card.model_dump(mode="json"),
                        }
                    )

            yield _sse_event(
                {
                    "type": "done",
                    "full_content": accumulated,
                    "assistant_turn_id": str(prep.assistant_turn_id),
                    "assistant_candidate_id": str(prep.assistant_candidate_id),
                }
            )

            chat_history_for_suggestions = (
                _text_only_chat_history(history_messages)
                + [{"role": "user", "content": outbound_user_content}]
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
                    character_id=str(prep.character_id),
                ),
                logger=logger,
                suggestions_error_log_message="Reply suggestions generation failed",
                reply_card_core_coro=(
                    self._chat_learning_service.generate_reply_card(
                        assistant_text=accumulated,
                        trace_context=workflow_trace_context,
                        user_id=str(current_user.id),
                        chat_id=str(prep.chat_id),
                        turn_id=str(prep.assistant_turn_id),
                        candidate_id=str(prep.assistant_candidate_id),
                        character_id=str(prep.character_id),
                    )
                    if should_generate_card
                    else None
                ),
                reply_card_error_log_message="Reply card generation failed",
            )

            # 6) Fire-and-forget memory management as soon as text generation is done.
            if memory_active:
                try:
                    spawn_bg_task(
                        self._memory_repo.manage(
                            user_text=outbound_user_content,
                            assistant_text=accumulated,
                            user_id=str(current_user.id),
                            character_id=str(prep.character_id),
                            chat_id=str(prep.chat_id),
                        ),
                        name="memory.manage_async",
                        ctx={
                            "user_id": str(current_user.id),
                            "character_id": str(prep.character_id),
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
                            character_id=str(prep.character_id),
                            chat_id=str(prep.chat_id),
                        ),
                        name="memory.judge_and_assign_narrative_group_async",
                        ctx={
                            "user_id": str(current_user.id),
                            "character_id": str(prep.character_id),
                            "chat_id": str(prep.chat_id),
                        },
                    )
                    yield _sse_event({"type": "memory_queued", "status": "queued"})
                except Exception:
                    logger.exception("Memory queue scheduling failed")

            if tts_enabled and tts_worker_task is not None:
                deadline = time.monotonic() + 60.0
                while not tts_worker_done:
                    for frame in _drain_tts_frames():
                        yield frame
                    for frame in await _drain_post_done_frames():
                        yield frame
                    for frame in _drain_title_frames():
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
                            for frame in await _render_post_done_item(next_item):
                                yield frame
                            continue

                    next_frame = await _wait_tts_frame(min(0.05, remaining))
                    if next_frame:
                        yield next_frame
                    elif tts_worker_task.done():
                        for frame in _drain_tts_frames():
                            yield frame
                        for frame in _drain_title_frames():
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
                    for frame in _drain_title_frames():
                        yield frame
                    if not post_done_tasks.pending:
                        break
                    next_item = await post_done_tasks.wait_for_item(0.05)
                    if next_item is None:
                        continue
                    for frame in await _render_post_done_item(next_item):
                        yield frame

            if title_task is not None and not title_event_emitted:
                try:
                    await title_task
                except Exception:
                    logger.exception(
                        "Chat title generation failed after stream; chat_id=%s", prep.chat_id
                    )
                for frame in _drain_title_frames():
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
            await self._finalize_stream_error(
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
            await self._finalize_stream_error(
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
            await self._finalize_stream_error(
                prep=prep,
                code="llm_service_error",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "llm_service_error", "message": message})
        except Exception as e:
            logger.exception(
                "Chat stream error user=%s chat=%s has_images=%s error_type=%s: %s",
                current_user.id, prep.chat_id, prep.has_images, type(e).__name__, e,
            )
            message = f"chat stream failed: {type(e).__name__} — {e}"
            update_observation_safe(
                workflow_observation,
                level="ERROR",
                status_message=message,
                output={
                    "status": "error",
                    "code": "chat_stream_failed",
                    "error_type": type(e).__name__,
                },
            )
            await self._rollback_after_stream_exception(reason="chat_stream_failed")
            await self._finalize_stream_error(
                prep=prep,
                code="chat_stream_failed",
                message=message,
                partial_content=accumulated,
            )
            yield _sse_event({"type": "error", "code": "chat_stream_failed", "message": message})
        finally:
            if post_done_tasks is not None:
                await post_done_tasks.aclose()
            if title_task and not title_task.done():
                title_task.cancel()
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

    async def _generate_and_persist_chat_title(
        self,
        *,
        current_user_id: uuid.UUID,
        chat_id: uuid.UUID,
        first_user_message: str,
        trace_context: dict[str, str] | None = None,
    ) -> str | None:
        try:
            title = await self._chat_title_service.generate_title(
                first_user_message,
                trace_context=trace_context,
                user_id=str(current_user_id),
                chat_id=str(chat_id),
            )
            if not title or title == _CHAT_TITLE_DEFAULT:
                return None

            async with AsyncSessionLocal() as session:
                session.info["current_user_id"] = str(current_user_id)
                repo = ChatRepository(session)
                uow = UnitOfWork(session)
                async with uow.transactional():
                    updated_chat = await repo.update_title_if_first_user_turn(
                        chat_id=chat_id,
                        title=title,
                        default_title=_CHAT_TITLE_DEFAULT,
                    )

            if updated_chat is None:
                return None
            return updated_chat.title
        except Exception:
            logger.exception("Auto title generation failed; chat_id=%s", chat_id)
            return None

    async def _prepare_stream(
        self,
        current_user: User,
        chat_id: uuid.UUID,
        content: str,
        images: list[ChatImageRef] | None = None,
    ) -> _StreamPrep:
        timing: dict[str, float] = {}
        t0 = time.monotonic()
        # Lock the chat row to serialize turn_no assignment for this chat.
        try:
            chat = await self._chat_repo.get_for_update(chat_id)
            timing["prep_db_lock_chat"] = time.monotonic() - t0
            if not chat:
                raise APIError(
                    status_code=404,
                    code="chat_not_found",
                    message=f"resource not found: chat does not exist; chat_id={chat_id}",
                )

            if chat.user_id != current_user.id:
                raise APIError(
                    status_code=403,
                    code="chat_forbidden",
                    message=f"authorization failed: chat is not accessible; chat_id={chat_id}",
                )

            if chat.state != ChatState.ACTIVE.value:
                raise APIError(
                    status_code=409,
                    code="chat_state_conflict",
                    message=(
                        "resource conflict: chat is not active; "
                        f"chat_id={chat_id}; state={chat.state}"
                    ),
                )

            t_char = time.monotonic()
            character = await self._character_repo.get_by_id(chat.character_id)
            timing["prep_db_character"] = time.monotonic() - t_char
            if not character:
                raise APIError(
                    status_code=404,
                    code="character_not_found",
                    message=f"resource not found: character does not exist; character_id={chat.character_id}",
                )
            assert_character_chat_writable(character, user_id=current_user.id)
            t_route = time.monotonic()
            resolved_llm_route = await self._character_llm_service.resolve_runtime_route(character)
            timing["prep_resolve_route"] = time.monotonic() - t_route
            normalized_images = _normalize_requested_chat_images(
                images,
                owner_id=str(current_user.id),
            )

            now = _now_utc()
            t_title_check = time.monotonic()
            should_generate_title = (
                chat.title == _CHAT_TITLE_DEFAULT
                and await self._turn_repo.count_by_chat_and_author(
                    chat_id=chat.id,
                    author_type=AuthorType.USER.value,
                )
                == 0
            )
            timing["prep_db_title_check"] = time.monotonic() - t_title_check

            history_leaf_turn_id = chat.active_leaf_turn_id or chat.last_turn_id
            parent_candidate_id: uuid.UUID | None = None
            if history_leaf_turn_id is not None:
                t_parent = time.monotonic()
                parent_turn = await self._turn_repo.get_by_id(history_leaf_turn_id)
                timing["prep_db_parent_turn"] = time.monotonic() - t_parent
                if not parent_turn or parent_turn.chat_id != chat.id:
                    raise APIError(
                        status_code=409,
                        code="chat_state_conflict",
                        message=(
                            "resource conflict: chat active leaf is invalid; "
                            f"chat_id={chat.id}; active_leaf_turn_id={history_leaf_turn_id}"
                        ),
                    )
                if parent_turn.primary_candidate_id is None:
                    raise APIError(
                        status_code=409,
                        code="chat_state_conflict",
                        message=(
                            "resource conflict: chat active leaf has no primary candidate; "
                            f"chat_id={chat.id}; active_leaf_turn_id={history_leaf_turn_id}"
                        ),
                    )
                parent_candidate_id = parent_turn.primary_candidate_id

            # Compute next turn numbers within the lock (prefer chats.last_turn_no).
            last_no = int(chat.last_turn_no or 0)
            if chat.last_turn_no is None:
                last_no = await self._turn_repo.get_max_turn_no(chat_id=chat.id)
                chat.last_turn_no = last_no
            user_turn_no = last_no + 1
            assistant_turn_no = last_no + 2

            t_db_writes = time.monotonic()

            # Insert user turn + candidate.
            user_turn = Turn(
                chat_id=chat.id,
                turn_no=user_turn_no,
                parent_turn_id=history_leaf_turn_id,
                parent_candidate_id=parent_candidate_id,
                author_type=AuthorType.USER.value,
                author_user_id=current_user.id,
                state=TurnState.OK.value,
                is_proactive=False,
            )
            self._turn_repo.add(user_turn)
            await self._uow.flush()

            cand_extra = None
            if normalized_images:
                cand_extra = {
                    "chat_images": [{"image_key": img.image_key} for img in normalized_images]
                }

            user_cand = Candidate(
                turn_id=user_turn.id,
                candidate_no=1,
                content=content,
                is_final=True,
                extra=cand_extra,
            )
            self._candidate_repo.add(user_cand)
            await self._uow.flush()
            user_turn.primary_candidate_id = user_cand.id

            # Insert assistant placeholder turn + candidate.
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

            # Advance chat pointers immediately so refresh mid-stream can hydrate the placeholder.
            chat.last_turn_no = assistant_turn_no
            if chat.origin == ChatOriginType.PROACTIVE.value:
                chat.last_read_turn_no = assistant_turn_no
            chat.last_turn_id = assistant_turn.id
            chat.active_leaf_turn_id = assistant_turn.id
            chat.last_turn_at = now

            await self._uow.commit()
            timing["prep_db_writes_commit"] = time.monotonic() - t_db_writes
        except Exception:
            await self._uow.rollback()
            raise

        # Ensure we have up-to-date objects.
        t_refresh = time.monotonic()
        await self._uow.refresh(chat)
        await self._uow.refresh(character)
        timing["prep_db_refresh"] = time.monotonic() - t_refresh
        timing["prep_total"] = time.monotonic() - t0

        return _StreamPrep(
            chat=chat,
            chat_id=chat.id,
            character=character,
            character_id=character.id,
            resolved_llm_route=resolved_llm_route,
            history_leaf_turn_id=history_leaf_turn_id,
            should_generate_title=should_generate_title,
            user_turn_id=user_turn.id,
            user_turn_no=user_turn_no,
            user_candidate_id=user_cand.id,
            assistant_turn_id=assistant_turn.id,
            assistant_turn_no=assistant_turn_no,
            assistant_candidate_id=assistant_cand.id,
            timing=timing,
            has_images=bool(normalized_images),
            chat_images=normalized_images,
        )

    async def _load_llm_history_messages_from_leaf(
        self,
        chat_id: uuid.UUID,
        leaf_turn_id: uuid.UUID | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Load history messages — delegates to the shared module-level function."""
        return await _load_llm_history_messages_from_leaf(
            turn_repo=self._turn_repo,
            candidate_repo=self._candidate_repo,
            chat_id=chat_id,
            leaf_turn_id=leaf_turn_id,
            limit=limit,
        )

    async def _finalize_stream_success(
        self,
        prep: _StreamPrep,
        *,
        current_user_id: uuid.UUID,
        user_message: str,
        full_content: str,
        model_type: str,
    ):
        now = _now_utc()
        try:
            cand = await self._candidate_repo.get_by_id(prep.assistant_candidate_id)
            turn = await self._turn_repo.get_by_id(prep.assistant_turn_id)
            chat = await self._chat_repo.get_by_id(prep.chat_id)
            character = await self._character_repo.get_by_id(prep.character_id)

            if not (cand and turn and chat and character):
                await self._uow.rollback()
                return None

            cand.content = full_content
            cand.is_final = True
            cand.model_type = model_type

            turn.state = TurnState.OK.value

            chat.last_turn_id = turn.id
            chat.active_leaf_turn_id = turn.id
            chat.last_turn_at = now

            growth_update = None
            if self._growth_service is not None:
                growth_update = await self._growth_service.record_canonical_chat_completed(
                    user_id=current_user_id,
                    character_id=prep.character_id,
                    chat_id=prep.chat_id,
                    user_message=user_message,
                    assistant_message=full_content,
                    occurred_at=now,
                )

            await self._uow.commit()
            return growth_update
        except Exception:
            await self._uow.rollback()
            raise

    async def _finalize_stream_error(
        self,
        prep: _StreamPrep,
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
            try:
                await self._uow.rollback()
            except Exception:
                logger.exception(
                    "Rollback failed while finalizing stream error; assistant_candidate_id=%s",
                    prep.assistant_candidate_id,
                )
            logger.exception(
                "Finalize stream error state failed; assistant_candidate_id=%s; code=%s",
                prep.assistant_candidate_id,
                code,
            )

    async def _rollback_after_stream_exception(self, *, reason: str) -> None:
        try:
            await self._uow.rollback()
        except Exception:
            logger.exception("Rollback failed after stream exception; reason=%s", reason)

    async def list_sidebar_characters(
        self,
        current_user: User,
        *,
        skip: int,
        limit: int,
    ) -> list[CharacterBrief]:
        rows = await self._chat_repo.list_characters_with_chats(
            current_user.id,
            skip=skip,
            limit=limit,
        )
        return [
            _character_brief(character, creator_username=username, has_unread_proactive=unread)
            for character, username, _, unread in rows
        ]
