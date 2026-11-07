"""Renaming persisted character-forge sessions."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from src.core.exceptions import APIError
from src.db.models import CharacterForgeSession
from src.services.character_forge_service import CharacterForgeService


class _FakeRepo:
    def __init__(self, session: CharacterForgeSession):
        self.session = session
        self.titles: list[str] = []

    async def get_session(self, session_id: uuid.UUID, *, user_id: uuid.UUID):
        if session_id != self.session.id or user_id != self.session.user_id:
            return None
        return self.session

    async def touch_session(self, session: CharacterForgeSession, **kwargs):
        for key, value in kwargs.items():
            setattr(session, key, value)
        if "title" in kwargs:
            self.titles.append(kwargs["title"])
        return session


def _build(session: CharacterForgeSession):
    repo = _FakeRepo(session)
    service = CharacterForgeService(
        repo=repo,
        tool_executor=SimpleNamespace(),
        llm_gateway_service=SimpleNamespace(),
        tts_gateway_service=SimpleNamespace(),
        exa_repository=SimpleNamespace(),
        character_service=SimpleNamespace(),
        chat_service=SimpleNamespace(),
    )
    return service, repo


async def test_rename_session_trims_title_and_persists():
    session = CharacterForgeSession(
        id=uuid.uuid4(), user_id=uuid.uuid4(), title="新角色"
    )
    service, repo = _build(session)
    user = SimpleNamespace(id=session.user_id)

    result = await service.rename_session(user, session.id, "  我的角色  ")

    assert session.title == "我的角色"
    assert result.title == "我的角色"
    assert result.id == str(session.id)
    assert repo.titles == ["我的角色"]


async def test_rename_session_rejects_blank_title():
    session = CharacterForgeSession(
        id=uuid.uuid4(), user_id=uuid.uuid4(), title="新角色"
    )
    service, repo = _build(session)
    user = SimpleNamespace(id=session.user_id)

    with pytest.raises(APIError) as exc:
        await service.rename_session(user, session.id, "   ")

    assert exc.value.status_code == 422
    assert exc.value.code == "validation_error"
    assert repo.titles == []
    assert session.title == "新角色"


async def test_rename_session_rejects_other_users_session():
    session = CharacterForgeSession(
        id=uuid.uuid4(), user_id=uuid.uuid4(), title="新角色"
    )
    service, repo = _build(session)
    other_user = SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(APIError) as exc:
        await service.rename_session(other_user, session.id, "别人的标题")

    assert exc.value.status_code == 404
    assert exc.value.code == "resource_not_found"
    assert repo.titles == []
