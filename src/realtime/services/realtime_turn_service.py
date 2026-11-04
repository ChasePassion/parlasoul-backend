"""Realtime turn persistence built on existing chat tree tables."""

from __future__ import annotations

import logging
import uuid

from src.core.exceptions import APIError
from src.db.models import (
    AuthorType,
    Candidate,
    CharacterStatusType,
    ChatOriginType,
    Turn,
    TurnState,
)
from src.db.session import AsyncSessionLocal
from src.realtime.session.models import CurrentTurnContext
from src.repositories.candidate_repository import CandidateRepository
from src.repositories.character_repository import CharacterRepository
from src.repositories.chat_repository import ChatRepository
from src.repositories.turn_repository import TurnRepository

logger = logging.getLogger(__name__)


def _merge_candidate_extra(candidate_extra: object, **fields: object) -> dict[str, object]:
    base = candidate_extra.copy() if isinstance(candidate_extra, dict) else {}
    for key, value in fields.items():
        base[key] = value
    return base


async def _resolve_leaf_from_turn(
    turn_repo: TurnRepository,
    *,
    chat_id: uuid.UUID,
    turn_id: uuid.UUID | None,
) -> Turn | None:
    if turn_id is None:
        return None

    cur_turn = await turn_repo.get_by_id(turn_id)
    seen: set[uuid.UUID] = set()
    while cur_turn is not None and cur_turn.id not in seen:
        seen.add(cur_turn.id)
        if cur_turn.chat_id != chat_id or cur_turn.primary_candidate_id is None:
            return cur_turn
        child = await turn_repo.get_child_by_parent_candidate(cur_turn.primary_candidate_id)
        if child is None or child.chat_id != chat_id:
            return cur_turn
        cur_turn = child
    return cur_turn


class RealtimeTurnService:
    async def begin_turn(
        self,
        *,
        user_id: uuid.UUID,
        chat_id: uuid.UUID,
        character_id: uuid.UUID,
        raw_user_text: str,
    ) -> CurrentTurnContext:
        async with AsyncSessionLocal() as db:
            db.info["current_user_id"] = str(user_id)
            chat_repo = ChatRepository(db)
            turn_repo = TurnRepository(db)
            candidate_repo = CandidateRepository(db)
            character_repo = CharacterRepository(db)

            chat = await chat_repo.get_for_update(chat_id)
            character = await character_repo.get_by_id(character_id)
            if chat is None or character is None:
                raise APIError(404, "resource_not_found", "resource not found: chat or character missing")
            if chat.user_id != user_id:
                raise APIError(403, "forbidden", "authorization failed: chat is not accessible")
            if chat.character_id != character_id:
                raise APIError(409, "resource_conflict", "resource conflict: chat and character mismatch")
            if getattr(character, "status", CharacterStatusType.ACTIVE.value) != CharacterStatusType.ACTIVE.value:
                raise APIError(409, "resource_conflict", "resource conflict: character is unpublished")

            history_leaf_turn = await _resolve_leaf_from_turn(
                turn_repo,
                chat_id=chat.id,
                turn_id=chat.active_leaf_turn_id or chat.last_turn_id,
            )
            history_leaf_turn_id = history_leaf_turn.id if history_leaf_turn is not None else None
            if history_leaf_turn_id != (chat.active_leaf_turn_id or chat.last_turn_id):
                logger.info(
                    "Realtime chat leaf repaired before turn creation: chat_id=%s old_leaf_turn_id=%s new_leaf_turn_id=%s",
                    chat.id,
                    chat.active_leaf_turn_id or chat.last_turn_id,
                    history_leaf_turn_id,
                )
                chat.active_leaf_turn_id = history_leaf_turn_id
                chat.last_turn_id = history_leaf_turn_id

            parent_turn_id = history_leaf_turn_id
            parent_candidate_id = None
            if history_leaf_turn is not None and history_leaf_turn.primary_candidate_id:
                parent_candidate_id = history_leaf_turn.primary_candidate_id

            last_no = int(chat.last_turn_no or 0)
            if chat.last_turn_no is None:
                last_no = await turn_repo.get_max_turn_no(chat_id=chat.id)
            if history_leaf_turn is not None:
                last_no = max(last_no, int(history_leaf_turn.turn_no))
            user_turn_no = last_no + 1
            assistant_turn_no = user_turn_no + 1

            user_turn = Turn(
                chat_id=chat.id,
                turn_no=user_turn_no,
                parent_turn_id=parent_turn_id,
                parent_candidate_id=parent_candidate_id,
                author_type=AuthorType.USER.value,
                author_user_id=user_id,
                state=TurnState.OK.value,
                is_proactive=False,
            )
            turn_repo.add(user_turn)
            await db.flush()

            user_cand = Candidate(
                turn_id=user_turn.id,
                candidate_no=1,
                content=raw_user_text,
                model_type=None,
                is_final=True,
                extra=None,
            )
            candidate_repo.add(user_cand)
            await db.flush()
            user_turn.primary_candidate_id = user_cand.id

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
            turn_repo.add(assistant_turn)
            await db.flush()

            assistant_cand = Candidate(
                turn_id=assistant_turn.id,
                candidate_no=1,
                content="",
                model_type=None,
                is_final=False,
                extra={"source": "realtime_placeholder"},
            )
            candidate_repo.add(assistant_cand)
            await db.flush()
            assistant_turn.primary_candidate_id = assistant_cand.id

            chat.last_turn_no = assistant_turn_no
            if getattr(chat, "origin", None) == ChatOriginType.PROACTIVE.value:
                chat.last_read_turn_no = assistant_turn_no
            chat.last_turn_id = assistant_turn.id
            chat.active_leaf_turn_id = assistant_turn.id

            await db.commit()

            return CurrentTurnContext(
                history_leaf_turn_id=history_leaf_turn_id,
                user_turn_id=user_turn.id,
                user_candidate_id=user_cand.id,
                assistant_turn_id=assistant_turn.id,
                assistant_candidate_id=assistant_cand.id,
                raw_user_text=raw_user_text,
            )

    async def finalize_assistant_success(self, turn_ctx: CurrentTurnContext, *, user_id: uuid.UUID) -> None:
        async with AsyncSessionLocal() as db:
            db.info["current_user_id"] = str(user_id)
            candidate_repo = CandidateRepository(db)
            turn_repo = TurnRepository(db)
            chat_repo = ChatRepository(db)
            cand = await candidate_repo.get_by_id(turn_ctx.assistant_candidate_id)
            turn = await turn_repo.get_by_id(turn_ctx.assistant_turn_id)
            chat = await chat_repo.get_for_update(turn.chat_id) if turn else None
            if cand is None or turn is None or chat is None:
                return
            cand.content = turn_ctx.assistant_accumulated_text
            cand.is_final = True

            resolved_leaf = await _resolve_leaf_from_turn(
                turn_repo,
                chat_id=chat.id,
                turn_id=turn.id,
            )
            if resolved_leaf is not None:
                if resolved_leaf.id != turn.id:
                    logger.info(
                        "Realtime finalize skipped stale leaf rewind: chat_id=%s finalized_turn_id=%s resolved_leaf_turn_id=%s",
                        chat.id,
                        turn.id,
                        resolved_leaf.id,
                    )
                chat.active_leaf_turn_id = resolved_leaf.id
                chat.last_turn_id = resolved_leaf.id
            await db.commit()

    async def persist_assistant_partial(
        self,
        *,
        assistant_turn_id: uuid.UUID,
        assistant_candidate_id: uuid.UUID,
        content: str,
        user_id: uuid.UUID,
    ) -> bool:
        async with AsyncSessionLocal() as db:
            db.info["current_user_id"] = str(user_id)
            candidate_repo = CandidateRepository(db)
            turn_repo = TurnRepository(db)
            cand = await candidate_repo.get_by_id(assistant_candidate_id)
            turn = await turn_repo.get_by_id(assistant_turn_id)
            if cand is None or turn is None:
                return False
            if not content.strip():
                return False
            cand.content = content
            cand.is_final = False
            await db.commit()
            return True

    async def finalize_assistant_interrupted(self, turn_ctx: CurrentTurnContext, *, user_id: uuid.UUID) -> bool:
        async with AsyncSessionLocal() as db:
            db.info["current_user_id"] = str(user_id)
            candidate_repo = CandidateRepository(db)
            turn_repo = TurnRepository(db)
            cand = await candidate_repo.get_by_id(turn_ctx.assistant_candidate_id)
            turn = await turn_repo.get_by_id(turn_ctx.assistant_turn_id)
            if cand is None or turn is None:
                return False
            persisted_content = (cand.content or "").strip()
            if turn_ctx.assistant_accumulated_text.strip() and turn_ctx.llm_completed:
                cand.content = turn_ctx.assistant_accumulated_text
                cand.is_final = True
                turn.state = TurnState.OK.value
                cand.extra = _merge_candidate_extra(cand.extra, interrupted=True)
                await db.commit()
                return True
            if persisted_content:
                cand.content = persisted_content
                cand.is_final = True
                turn.state = TurnState.OK.value
                cand.extra = _merge_candidate_extra(cand.extra, interrupted=True)
            else:
                cand.content = ""
                cand.is_final = False
                turn.state = TurnState.ERROR.value
            await db.commit()
            return cand.is_final

    async def finalize_assistant_error(self, turn_ctx: CurrentTurnContext, message: str, *, user_id: uuid.UUID) -> None:
        async with AsyncSessionLocal() as db:
            db.info["current_user_id"] = str(user_id)
            candidate_repo = CandidateRepository(db)
            turn_repo = TurnRepository(db)
            cand = await candidate_repo.get_by_id(turn_ctx.assistant_candidate_id)
            turn = await turn_repo.get_by_id(turn_ctx.assistant_turn_id)
            if cand is None or turn is None:
                return
            cand.content = turn_ctx.assistant_accumulated_text or f"Error: {message}"
            cand.is_final = False
            turn.state = TurnState.ERROR.value
            await db.commit()
