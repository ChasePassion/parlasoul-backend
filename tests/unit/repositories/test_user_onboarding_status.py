from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.db.models import User
from src.repositories.user_repository import UserRepository


def _user(*, status: str) -> User:
    return User(
        id=uuid.uuid4(),
        email="new-user@example.com",
        onboarding_status=status,
    )


@pytest.mark.asyncio
async def test_update_onboarding_status_allows_forward_transition() -> None:
    user = _user(status="pending")
    session = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    repo = UserRepository(session)

    updated = await repo.update_onboarding_status(user, status="discover_done")

    assert updated is user
    assert user.onboarding_status == "discover_done"
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once_with(user)


@pytest.mark.parametrize(
    ("initial_status", "requested_status"),
    [("completed", "skipped"), ("discover_done", "discover_done")],
    ids=["terminal_state", "same_status"],
)
@pytest.mark.asyncio
async def test_update_onboarding_status_skips_terminal_or_unchanged(
    initial_status: str, requested_status: str
) -> None:
    user = _user(status=initial_status)
    session = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    repo = UserRepository(session)

    updated = await repo.update_onboarding_status(user, status=requested_status)

    assert updated is user
    assert user.onboarding_status == initial_status
    session.commit.assert_not_awaited()
    session.refresh.assert_not_awaited()
