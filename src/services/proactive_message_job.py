"""Orchestrates one proactive message dispatch from selection to persistence."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from src.core.config import settings
from src.core.exceptions import APIError
from src.core.uow import UnitOfWork
from src.db.models import CharacterStatusType, ProactiveMessageDispatch, User
from src.memory_system.exceptions import LLMAuthenticationError, LLMCallError
from src.repositories.character_repository import CharacterRepository
from src.repositories.memory_repository import MemoryRepository
from src.repositories.proactive_dispatch_repository import ProactiveDispatchRepository
from src.repositories.turn_repository import TurnRepository
from src.repositories.user_repository import UserRepository
from src.repositories.user_settings_repository import UserSettingsRepository
from src.services.bg_tasks import spawn_bg_task
from src.services.character_llm_service import CharacterLLMService
from src.services.chat_service import ChatService
from src.services.proactive_character_selector import ProactiveCharacterSelector
from src.services.proactive_prompt_builder import build_proactive_messages

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(UTC)


class ProactiveMessageJob:
    """Processes claimed proactive dispatch queue rows."""

    def __init__(
        self,
        *,
        uow: UnitOfWork,
        dispatch_repo: ProactiveDispatchRepository,
        user_repo: UserRepository,
        user_settings_repo: UserSettingsRepository,
        character_repo: CharacterRepository,
        turn_repo: TurnRepository,
        memory_repo: MemoryRepository,
        selector: ProactiveCharacterSelector,
        chat_service: ChatService,
        character_llm_service: CharacterLLMService,
    ) -> None:
        self._uow = uow
        self._dispatch_repo = dispatch_repo
        self._user_repo = user_repo
        self._user_settings_repo = user_settings_repo
        self._character_repo = character_repo
        self._turn_repo = turn_repo
        self._memory_repo = memory_repo
        self._selector = selector
        self._chat_service = chat_service
        self._character_llm_service = character_llm_service

    async def process_claimed_dispatch(self, dispatch: ProactiveMessageDispatch) -> None:
        now = _now_utc()
        user = await self._user_repo.get_by_id(dispatch.user_id)
        if user is None:
            await self._dispatch_repo.mark_skipped(
                dispatch,
                error_code="resource_not_found",
                error_message="user not found",
                now=now,
            )
            return

        user_settings = await self._user_settings_repo.get_or_create(
            user.id,
            default_font_size=settings.USER_SETTINGS_MESSAGE_FONT_SIZE_DEFAULT,
            default_display_mode=settings.USER_SETTINGS_DISPLAY_MODE_DEFAULT,
            default_memory_enabled=True,
            default_reply_card_enabled=settings.USER_SETTINGS_REPLY_CARD_ENABLED_DEFAULT,
            default_mixed_input_auto_translate_enabled=(
                settings.USER_SETTINGS_MIXED_INPUT_AUTO_TRANSLATE_ENABLED_DEFAULT
            ),
            default_auto_read_aloud_enabled=settings.USER_SETTINGS_AUTO_READ_ALOUD_ENABLED_DEFAULT,
            default_preferred_expression_bias_enabled=(
                settings.USER_SETTINGS_PREFERRED_EXPRESSION_BIAS_ENABLED_DEFAULT
            ),
            default_proactive_enabled=settings.USER_SETTINGS_PROACTIVE_ENABLED_DEFAULT,
            default_timezone=settings.USER_SETTINGS_TIMEZONE_DEFAULT,
        )

        if not user_settings.proactive_enabled:
            await self._dispatch_repo.mark_skipped(
                dispatch,
                error_code="resource_conflict",
                error_message="proactive messaging disabled",
                now=now,
            )
            return

        last_turn_at = await self._turn_repo.get_last_activity_at(user_id=user.id)
        last_seen_at = last_turn_at or user.last_login_at or user.created_at or now
        if (now - last_seen_at) > timedelta(days=7):
            await self._dispatch_repo.mark_skipped(
                dispatch,
                error_code="resource_conflict",
                error_message="user inactive for more than 7 days",
                now=now,
            )
            return

        selection = await self._selector.select(user.id)
        if selection.selected_character_id is None:
            await self._dispatch_repo.mark_skipped(
                dispatch,
                error_code="resource_not_found",
                error_message="no eligible proactive character",
                now=now,
            )
            return

        dispatch.selected_character_id = selection.selected_character_id
        character = await self._character_repo.get_by_id(selection.selected_character_id)
        if character is None or character.status != CharacterStatusType.ACTIVE.value:
            await self._dispatch_repo.mark_skipped(
                dispatch,
                error_code="resource_not_found",
                error_message="selected character unavailable",
                now=now,
            )
            return

        last_interaction_at = await self._turn_repo.get_last_interaction_at(
            user_id=user.id,
            character_id=character.id,
        )
        last_interaction_days = 0
        if last_interaction_at is not None:
            last_interaction_days = max(0, (now - last_interaction_at).days)

        memories = await self._memory_repo.list_for_proactive_prompt(
            user_id=str(user.id),
            character_id=str(character.id),
            semantic_limit=1000,
            episodic_limit=1000,
        )
        total_memory_count = len(memories.get("episodic", [])) + len(memories.get("semantic", []))
        if total_memory_count < settings.PROACTIVE_JOB_MIN_MEMORY_COUNT:
            await self._dispatch_repo.mark_skipped(
                dispatch,
                error_code="insufficient_memories",
                error_message=(
                    f"memory count {total_memory_count} below threshold "
                    f"{settings.PROACTIVE_JOB_MIN_MEMORY_COUNT}"
                ),
                now=now,
            )
            return
        user_name = _user_name(user)
        messages = build_proactive_messages(
            character_system_prompt=character.system_prompt,
            dialogue_style_id=getattr(character, "dialogue_style_id", None),
            memories=memories,
            user_name=user_name,
            last_interaction_days=last_interaction_days,
            max_episodic_memories=settings.PROACTIVE_JOB_MAX_EPISODIC_MEMORIES,
        )

        try:
            route = await self._character_llm_service.resolve_runtime_route(character)
            proactive_message = await self._character_llm_service.generate_text(
                route=route,
                system_prompt=messages[0]["content"],
                user_message=messages[1]["content"],
                operation="chat.proactive.outreach",
                route_role="chat",
                user_id=str(user.id),
                character_id=str(character.id),
            )
        except Exception as exc:
            if _is_retryable_generation_error(exc) and dispatch.attempt_count < settings.PROACTIVE_JOB_MAX_RETRIES:
                retry_delay = settings.PROACTIVE_JOB_RETRY_BASE_DELAY_SECONDS * (
                    2 ** max(0, dispatch.attempt_count - 1)
                )
                await self._dispatch_repo.schedule_retry(
                    dispatch,
                    error_code=_error_code_from_exception(exc),
                    error_message=str(exc),
                    next_attempt_at=now + timedelta(seconds=retry_delay),
                    selected_character_id=character.id,
                )
                logger.warning(
                    "Scheduled proactive dispatch retry.",
                    extra={
                        "event": "proactive.dispatch.retry_scheduled",
                        "user_id": str(user.id),
                        "character_id": str(character.id),
                    },
                )
                return

            await self._dispatch_repo.mark_failed(
                dispatch,
                error_code=_error_code_from_exception(exc),
                error_message=str(exc),
                now=now,
            )
            logger.exception(
                "Failed to generate proactive message. user_id=%s character_id=%s dispatch_id=%s",
                user.id,
                character.id,
                dispatch.id,
            )
            return

        chat, turn, candidate = await self._chat_service.create_proactive_chat(
            current_user=user,
            character=character,
            content=proactive_message,
            meta={"dispatch_id": str(dispatch.id)},
        )
        spawn_bg_task(
            self._memory_repo.record_proactive_outreach(
                proactive_message=proactive_message,
                user_id=str(user.id),
                character_id=str(character.id),
                chat_id=str(chat.id),
            ),
            name="proactive.record_outreach",
            ctx={"dispatch_id": str(dispatch.id), "chat_id": str(chat.id)},
        )

        await self._dispatch_repo.mark_succeeded(
            dispatch,
            selected_character_id=character.id,
            payload_json={
                "chat_id": str(chat.id),
                "turn_id": str(turn.id),
                "candidate_id": str(candidate.id),
                "weights": selection.weights,
            },
            now=now,
        )
        logger.info(
            "Completed proactive dispatch.",
            extra={
                "event": "proactive.dispatch.completed",
                "user_id": str(user.id),
                "chat_id": str(chat.id),
                "character_id": str(character.id),
            },
        )


def _user_name(user: User) -> str:
    for candidate in (user.username, user.display_name, user.email):
        if candidate and candidate.strip():
            if "@" in candidate:
                return candidate.split("@", 1)[0]
            return candidate.strip()
    return "the user"


def _is_retryable_generation_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, LLMCallError)):
        return True
    if isinstance(exc, LLMAuthenticationError):
        return False
    if isinstance(exc, APIError):
        return exc.code in {"external_timeout", "llm_service_error"}
    return False


def _error_code_from_exception(exc: Exception) -> str:
    if isinstance(exc, APIError):
        return exc.code
    if isinstance(exc, LLMAuthenticationError):
        return "llm_service_error"
    if isinstance(exc, LLMCallError):
        return "llm_service_error"
    if isinstance(exc, TimeoutError):
        return "external_timeout"
    return "internal_error"
