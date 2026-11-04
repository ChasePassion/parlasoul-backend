from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.schemas import ConsolidateRequest, ManageRequest, ResetRequest, SearchRequest
from src.core.exceptions import APIError
from src.db.models import User
from src.repositories.memory_repository import MemoryRepository
from src.services.character_access_service import CharacterAccessService
from src.services.memory_service import MemoryService


def _user() -> User:
    return User(id=uuid.uuid4(), email="u@example.com")


@pytest.mark.asyncio
async def test_manage_success() -> None:
    memory_repo = MemoryRepository.__new__(MemoryRepository)
    memory_repo.manage = AsyncMock(return_value=[1, 2])
    access_service = CharacterAccessService.__new__(CharacterAccessService)
    access_service.assert_access = AsyncMock()

    service = MemoryService(memory_repo, access_service)
    request = ManageRequest(
        character_id=str(uuid.uuid4()),
        chat_id=str(uuid.uuid4()),
        user_text="hello",
        assistant_text="world",
    )

    result = await service.manage(request=request, current_user=_user())

    assert result.success is True
    assert result.added_ids == [1, 2]
    access_service.assert_access.assert_awaited_once()
    memory_repo.manage.assert_awaited_once()


@pytest.mark.asyncio
async def test_search_wraps_unexpected_error() -> None:
    memory_repo = MemoryRepository.__new__(MemoryRepository)
    memory_repo.search = AsyncMock(side_effect=RuntimeError("boom"))
    access_service = CharacterAccessService.__new__(CharacterAccessService)
    access_service.assert_access = AsyncMock()
    service = MemoryService(memory_repo, access_service)

    request = SearchRequest(
        character_id=str(uuid.uuid4()),
        chat_id=str(uuid.uuid4()),
        query="q",
    )
    with pytest.raises(APIError) as exc:
        await service.search(request=request, current_user=_user())

    assert exc.value.status_code == 500
    assert exc.value.code == "memory_search_failed"


@pytest.mark.asyncio
async def test_delete_not_found() -> None:
    memory_repo = MemoryRepository.__new__(MemoryRepository)
    memory_repo.delete = AsyncMock(return_value=False)
    access_service = CharacterAccessService.__new__(CharacterAccessService)
    access_service.assert_access = AsyncMock()
    service = MemoryService(memory_repo, access_service)

    with pytest.raises(APIError) as exc:
        await service.delete(
            memory_id=123,
            character_id=str(uuid.uuid4()),
            current_user=_user(),
        )

    assert exc.value.status_code == 404
    assert exc.value.code == "memory_not_found"
    memory_repo.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_success() -> None:
    memory_repo = MemoryRepository.__new__(MemoryRepository)
    memory_repo.delete = AsyncMock(return_value=True)
    access_service = CharacterAccessService.__new__(CharacterAccessService)
    access_service.assert_access = AsyncMock()
    service = MemoryService(memory_repo, access_service)

    result = await service.delete(
        memory_id=123,
        character_id=str(uuid.uuid4()),
        current_user=_user(),
    )

    assert result.success is True
    assert result.deleted_count == 1
    memory_repo.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_wraps_unexpected_error() -> None:
    memory_repo = MemoryRepository.__new__(MemoryRepository)
    memory_repo.delete = AsyncMock(side_effect=RuntimeError("boom"))
    access_service = CharacterAccessService.__new__(CharacterAccessService)
    access_service.assert_access = AsyncMock()
    service = MemoryService(memory_repo, access_service)

    with pytest.raises(APIError) as exc:
        await service.delete(
            memory_id=123,
            character_id=str(uuid.uuid4()),
            current_user=_user(),
        )

    assert exc.value.status_code == 500
    assert exc.value.code == "memory_delete_failed"
    memory_repo.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_consolidate_success() -> None:
    stats = SimpleNamespace(memories_processed=7, semantic_created=3)
    memory_repo = MemoryRepository.__new__(MemoryRepository)
    memory_repo.consolidate = AsyncMock(return_value=stats)
    access_service = CharacterAccessService.__new__(CharacterAccessService)
    access_service.assert_access = AsyncMock()
    service = MemoryService(memory_repo, access_service)

    result = await service.consolidate(
        request=ConsolidateRequest(character_id=str(uuid.uuid4())),
        current_user=_user(),
    )

    assert result.memories_processed == 7
    assert result.semantic_created == 3


@pytest.mark.asyncio
async def test_reset_success() -> None:
    memory_repo = MemoryRepository.__new__(MemoryRepository)
    memory_repo.reset = AsyncMock(return_value=5)
    access_service = CharacterAccessService.__new__(CharacterAccessService)
    access_service.assert_access = AsyncMock()
    service = MemoryService(memory_repo, access_service)

    result = await service.reset(
        request=ResetRequest(character_id=str(uuid.uuid4())),
        current_user=_user(),
    )

    assert result.success is True
    assert result.deleted_count == 5
