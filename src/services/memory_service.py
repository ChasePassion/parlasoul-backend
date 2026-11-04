"""Memory service layer for memory APIs."""

from __future__ import annotations

import logging

from src.api.schemas import (
    ConsolidateRequest,
    ConsolidateResponse,
    DeleteResponse,
    ManageRequest,
    ManageResponse,
    MemoryResponse,
    ResetRequest,
    ResetResponse,
    SearchRequest,
    SearchResponse,
)
from src.core.exceptions import APIError
from src.db.models import User
from src.repositories.memory_repository import MemoryRepository
from src.services.character_access_service import CharacterAccessService

logger = logging.getLogger(__name__)


def _to_memory_response(record) -> MemoryResponse:
    return MemoryResponse(
        id=record.id,
        user_id=record.user_id,
        character_id=record.character_id,
        memory_type=record.memory_type,
        ts=record.ts,
        chat_id=record.chat_id,
        text=record.text,
        group_id=record.group_id,
    )


class MemoryService:
    """Business logic for memory management endpoints."""

    def __init__(
        self,
        memory_repo: MemoryRepository,
        character_access_service: CharacterAccessService,
    ):
        self._memory_repo = memory_repo
        self._character_access_service = character_access_service

    async def manage(
        self,
        *,
        request: ManageRequest,
        current_user: User,
    ) -> ManageResponse:
        effective_user_id = str(current_user.id)
        await self._character_access_service.assert_access(
            current_user=current_user,
            character_id=request.character_id,
        )

        try:
            added_ids = await self._memory_repo.manage(
                user_text=request.user_text,
                assistant_text=request.assistant_text,
                user_id=effective_user_id,
                character_id=request.character_id,
                chat_id=request.chat_id,
            )
            logger.info(
                "Manage completed for user %s: added %s memories",
                effective_user_id,
                len(added_ids),
            )
            return ManageResponse(added_ids=added_ids, success=True)
        except APIError:
            raise
        except Exception as exc:
            logger.error("Manage failed for user %s: %s", effective_user_id, exc)
            raise APIError(
                status_code=500,
                code="memory_manage_failed",
                message="memory pipeline failed: manage operation failed",
            )

    async def search(
        self,
        *,
        request: SearchRequest,
        current_user: User,
    ) -> SearchResponse:
        effective_user_id = str(current_user.id)
        await self._character_access_service.assert_access(
            current_user=current_user,
            character_id=request.character_id,
        )

        try:
            result = await self._memory_repo.search(
                query=request.query,
                user_id=effective_user_id,
                character_id=request.character_id,
                chat_id=request.chat_id,
            )
        except APIError:
            raise
        except Exception as exc:
            logger.error("Search failed for user %s: %s", effective_user_id, exc)
            raise APIError(
                status_code=500,
                code="memory_search_failed",
                message="memory pipeline failed: search operation failed",
            )

        episodic = [_to_memory_response(item) for item in result.get("episodic", [])]
        semantic = [_to_memory_response(item) for item in result.get("semantic", [])]

        logger.info(
            "Search for user %s: found %s episodic, %s semantic",
            effective_user_id,
            len(episodic),
            len(semantic),
        )
        return SearchResponse(episodic=episodic, semantic=semantic)

    async def delete(
        self,
        *,
        memory_id: int,
        character_id: str,
        current_user: User,
    ) -> DeleteResponse:
        effective_user_id = str(current_user.id)
        await self._character_access_service.assert_access(
            current_user=current_user,
            character_id=character_id,
        )

        try:
            success = await self._memory_repo.delete(
                memory_id=memory_id,
                user_id=effective_user_id,
                character_id=character_id,
            )
            if not success:
                raise APIError(
                    status_code=404,
                    code="memory_not_found",
                    message=f"resource not found: memory does not exist; memory_id={memory_id}",
                )

            logger.info("Deleted memory %s for user %s", memory_id, effective_user_id)
            return DeleteResponse(success=True, deleted_count=1)
        except APIError:
            raise
        except Exception as exc:
            logger.error("Delete failed for user %s: %s", effective_user_id, exc)
            raise APIError(
                status_code=500,
                code="memory_delete_failed",
                message="memory pipeline failed: delete operation failed",
            )

    async def reset(
        self,
        *,
        request: ResetRequest,
        current_user: User,
    ) -> ResetResponse:
        effective_user_id = str(current_user.id)
        await self._character_access_service.assert_access(
            current_user=current_user,
            character_id=request.character_id,
        )

        try:
            deleted_count = await self._memory_repo.reset(
                user_id=effective_user_id,
                character_id=request.character_id,
            )
        except APIError:
            raise
        except Exception as exc:
            logger.error("Reset failed for user %s: %s", effective_user_id, exc)
            raise APIError(
                status_code=500,
                code="memory_reset_failed",
                message="memory pipeline failed: reset operation failed",
            )

        logger.info(
            "Reset completed for user %s: deleted %s memories",
            effective_user_id,
            deleted_count,
        )
        return ResetResponse(success=True, deleted_count=deleted_count)

    async def consolidate(
        self,
        *,
        request: ConsolidateRequest,
        current_user: User,
    ) -> ConsolidateResponse:
        effective_user_id = str(current_user.id)
        await self._character_access_service.assert_access(
            current_user=current_user,
            character_id=request.character_id,
        )

        try:
            stats = await self._memory_repo.consolidate(
                user_id=effective_user_id,
                character_id=request.character_id,
            )
        except APIError:
            raise
        except Exception as exc:
            logger.error("Consolidate failed for user %s: %s", effective_user_id, exc)
            raise APIError(
                status_code=500,
                code="memory_consolidate_failed",
                message="memory pipeline failed: consolidate operation failed",
            )

        logger.info(
            "Consolidation for user %s: processed %s, created %s",
            effective_user_id,
            stats.memories_processed,
            stats.semantic_created,
        )
        return ConsolidateResponse(
            memories_processed=stats.memories_processed,
            semantic_created=stats.semantic_created,
        )
