from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from src.core.exceptions import APIError
from src.db.models import TurnState, VisibilityType
from src.realtime.services import realtime_turn_service as realtime_turn_module
from src.realtime.services.realtime_turn_service import RealtimeTurnService
from src.realtime.session.models import CurrentTurnContext


class _FakeSession:
    def __init__(self) -> None:
        self.flush_count = 0
        self.commit_count = 0
        self.info: dict[str, object] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def flush(self) -> None:
        self.flush_count += 1

    async def commit(self) -> None:
        self.commit_count += 1


class _FakeChatRepo:
    def __init__(self, chat) -> None:
        self.chat = chat

    async def get_for_update(self, chat_id: uuid.UUID):
        return self.chat if self.chat.id == chat_id else None

    async def get_by_id(self, chat_id: uuid.UUID):
        return self.chat if self.chat.id == chat_id else None


class _FakeTurnRepo:
    def __init__(self, *, turns_by_id: dict[uuid.UUID, object], children_by_candidate: dict[uuid.UUID, object | None]) -> None:
        self.turns_by_id = turns_by_id
        self.children_by_candidate = children_by_candidate
        self.added: list[object] = []

    async def get_by_id(self, turn_id: uuid.UUID):
        return self.turns_by_id.get(turn_id)

    async def get_child_by_parent_candidate(self, parent_candidate_id: uuid.UUID):
        return self.children_by_candidate.get(parent_candidate_id)

    async def get_max_turn_no(self, *, chat_id: uuid.UUID) -> int:
        return max(int(turn.turn_no) for turn in self.turns_by_id.values() if turn.chat_id == chat_id)

    def add(self, turn) -> None:
        self.added.append(turn)
        self.turns_by_id[turn.id] = turn


class _FakeCandidateRepo:
    def __init__(self, *, candidates_by_id: dict[uuid.UUID, object] | None = None) -> None:
        self.candidates_by_id = candidates_by_id or {}
        self.added: list[object] = []

    async def get_by_id(self, candidate_id: uuid.UUID):
        return self.candidates_by_id.get(candidate_id)

    def add(self, candidate) -> None:
        self.added.append(candidate)
        self.candidates_by_id[candidate.id] = candidate


class _FakeCharacterRepo:
    def __init__(self, character) -> None:
        self.character = character

    async def get_by_id(self, character_id: uuid.UUID):
        return self.character if self.character.id == character_id else None


def _install_repos(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chat_repo: _FakeChatRepo,
    turn_repo: _FakeTurnRepo,
    candidate_repo: _FakeCandidateRepo,
    character_repo: _FakeCharacterRepo,
    session: _FakeSession,
) -> None:
    monkeypatch.setattr(realtime_turn_module, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(realtime_turn_module, "ChatRepository", lambda db: chat_repo)
    monkeypatch.setattr(realtime_turn_module, "TurnRepository", lambda db: turn_repo)
    monkeypatch.setattr(realtime_turn_module, "CandidateRepository", lambda db: candidate_repo)
    monkeypatch.setattr(realtime_turn_module, "CharacterRepository", lambda db: character_repo)


def _build_stale_leaf_chain():
    chat_id = uuid.uuid4()
    character_id = uuid.uuid4()
    user_id = uuid.uuid4()
    old_assistant_candidate_id = uuid.uuid4()
    user_candidate_id = uuid.uuid4()
    current_leaf_candidate_id = uuid.uuid4()

    stale_assistant_turn = SimpleNamespace(
        id=uuid.uuid4(),
        chat_id=chat_id,
        turn_no=455,
        parent_turn_id=uuid.uuid4(),
        primary_candidate_id=old_assistant_candidate_id,
    )
    existing_user_turn = SimpleNamespace(
        id=uuid.uuid4(),
        chat_id=chat_id,
        turn_no=456,
        parent_turn_id=stale_assistant_turn.id,
        parent_candidate_id=old_assistant_candidate_id,
        primary_candidate_id=user_candidate_id,
    )
    current_leaf_turn = SimpleNamespace(
        id=uuid.uuid4(),
        chat_id=chat_id,
        turn_no=457,
        parent_turn_id=existing_user_turn.id,
        parent_candidate_id=user_candidate_id,
        primary_candidate_id=current_leaf_candidate_id,
    )

    chat = SimpleNamespace(
        id=chat_id,
        user_id=user_id,
        character_id=character_id,
        active_leaf_turn_id=stale_assistant_turn.id,
        last_turn_id=stale_assistant_turn.id,
        last_turn_no=457,
    )
    character = SimpleNamespace(
        id=character_id,
        visibility=VisibilityType.PUBLIC.value,
    )
    turns_by_id = {
        stale_assistant_turn.id: stale_assistant_turn,
        existing_user_turn.id: existing_user_turn,
        current_leaf_turn.id: current_leaf_turn,
    }
    children_by_candidate = {
        old_assistant_candidate_id: existing_user_turn,
        user_candidate_id: current_leaf_turn,
        current_leaf_candidate_id: None,
    }
    return SimpleNamespace(
        chat=chat,
        character=character,
        user_id=user_id,
        stale_assistant_turn=stale_assistant_turn,
        existing_user_turn=existing_user_turn,
        current_leaf_turn=current_leaf_turn,
        turns_by_id=turns_by_id,
        children_by_candidate=children_by_candidate,
    )


@pytest.mark.asyncio
async def test_begin_turn_repairs_stale_chat_leaf_before_creating_user_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_stale_leaf_chain()
    session = _FakeSession()
    turn_repo = _FakeTurnRepo(
        turns_by_id=fixture.turns_by_id,
        children_by_candidate=fixture.children_by_candidate,
    )
    candidate_repo = _FakeCandidateRepo()
    _install_repos(
        monkeypatch,
        chat_repo=_FakeChatRepo(fixture.chat),
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=_FakeCharacterRepo(fixture.character),
        session=session,
    )

    turn_ctx = await RealtimeTurnService().begin_turn(
        user_id=fixture.user_id,
        chat_id=fixture.chat.id,
        character_id=fixture.character.id,
        raw_user_text="hello",
    )

    created_user_turn = turn_repo.added[0]
    created_assistant_turn = turn_repo.added[1]
    assert turn_ctx.history_leaf_turn_id == fixture.current_leaf_turn.id
    assert created_user_turn.parent_turn_id == fixture.current_leaf_turn.id
    assert created_user_turn.parent_candidate_id == fixture.current_leaf_turn.primary_candidate_id
    assert fixture.chat.active_leaf_turn_id == created_assistant_turn.id
    assert fixture.chat.last_turn_id == created_assistant_turn.id
    assert fixture.chat.last_turn_no == 459
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_finalize_success_advances_stale_leaf_to_existing_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_stale_leaf_chain()
    assistant_candidate_id = uuid.uuid4()
    candidate = SimpleNamespace(id=assistant_candidate_id, content="", is_final=False)
    session = _FakeSession()
    turn_repo = _FakeTurnRepo(
        turns_by_id=fixture.turns_by_id,
        children_by_candidate=fixture.children_by_candidate,
    )
    candidate_repo = _FakeCandidateRepo(candidates_by_id={assistant_candidate_id: candidate})
    _install_repos(
        monkeypatch,
        chat_repo=_FakeChatRepo(fixture.chat),
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=_FakeCharacterRepo(fixture.character),
        session=session,
    )

    await RealtimeTurnService().finalize_assistant_success(
        CurrentTurnContext(
            history_leaf_turn_id=uuid.uuid4(),
            user_turn_id=uuid.uuid4(),
            user_candidate_id=uuid.uuid4(),
            assistant_turn_id=fixture.stale_assistant_turn.id,
            assistant_candidate_id=assistant_candidate_id,
            raw_user_text="hello",
            assistant_accumulated_text="done",
        ),
        user_id=fixture.user_id,
    )

    assert candidate.content == "done"
    assert candidate.is_final is True
    assert fixture.chat.active_leaf_turn_id == fixture.current_leaf_turn.id
    assert fixture.chat.last_turn_id == fixture.current_leaf_turn.id
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_persist_assistant_partial_updates_existing_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_stale_leaf_chain()
    assistant_candidate_id = uuid.uuid4()
    candidate = SimpleNamespace(
        id=assistant_candidate_id,
        content="",
        is_final=False,
        extra={"source": "realtime_placeholder"},
    )
    session = _FakeSession()
    turn_repo = _FakeTurnRepo(
        turns_by_id=fixture.turns_by_id,
        children_by_candidate=fixture.children_by_candidate,
    )
    candidate_repo = _FakeCandidateRepo(candidates_by_id={assistant_candidate_id: candidate})
    _install_repos(
        monkeypatch,
        chat_repo=_FakeChatRepo(fixture.chat),
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=_FakeCharacterRepo(fixture.character),
        session=session,
    )

    persisted = await RealtimeTurnService().persist_assistant_partial(
        assistant_turn_id=fixture.stale_assistant_turn.id,
        assistant_candidate_id=assistant_candidate_id,
        content="Hello world.",
        user_id=fixture.user_id,
    )

    assert persisted is True
    assert candidate.content == "Hello world."
    assert candidate.is_final is False
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_finalize_interrupted_keeps_already_persisted_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_stale_leaf_chain()
    assistant_candidate_id = uuid.uuid4()
    candidate = SimpleNamespace(
        id=assistant_candidate_id,
        content="已持久化内容。",
        is_final=False,
        extra={"source": "realtime_placeholder"},
    )
    fixture.stale_assistant_turn.state = None
    session = _FakeSession()
    turn_repo = _FakeTurnRepo(
        turns_by_id=fixture.turns_by_id,
        children_by_candidate=fixture.children_by_candidate,
    )
    candidate_repo = _FakeCandidateRepo(candidates_by_id={assistant_candidate_id: candidate})
    _install_repos(
        monkeypatch,
        chat_repo=_FakeChatRepo(fixture.chat),
        turn_repo=turn_repo,
        candidate_repo=candidate_repo,
        character_repo=_FakeCharacterRepo(fixture.character),
        session=session,
    )

    is_final = await RealtimeTurnService().finalize_assistant_interrupted(
        CurrentTurnContext(
            history_leaf_turn_id=uuid.uuid4(),
            user_turn_id=uuid.uuid4(),
            user_candidate_id=uuid.uuid4(),
            assistant_turn_id=fixture.stale_assistant_turn.id,
            assistant_candidate_id=assistant_candidate_id,
            raw_user_text="hello",
            assistant_accumulated_text="已持久化内容。还没来得及持久化的尾巴",
            llm_completed=False,
            interrupted=True,
        ),
        user_id=fixture.user_id,
    )

    assert is_final is True
    assert candidate.content == "已持久化内容。"
    assert candidate.is_final is True
    assert candidate.extra["interrupted"] is True
    assert fixture.stale_assistant_turn.state == TurnState.OK.value
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_begin_turn_allows_creator_of_unpublished_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_stale_leaf_chain()
    fixture.character.visibility = VisibilityType.PRIVATE.value
    fixture.character.creator_id = fixture.user_id
    session = _FakeSession()
    turn_repo = _FakeTurnRepo(
        turns_by_id=fixture.turns_by_id,
        children_by_candidate=fixture.children_by_candidate,
    )
    _install_repos(
        monkeypatch,
        chat_repo=_FakeChatRepo(fixture.chat),
        turn_repo=turn_repo,
        candidate_repo=_FakeCandidateRepo(),
        character_repo=_FakeCharacterRepo(fixture.character),
        session=session,
    )

    turn_ctx = await RealtimeTurnService().begin_turn(
        user_id=fixture.user_id,
        chat_id=fixture.chat.id,
        character_id=fixture.character.id,
        raw_user_text="hello",
    )

    assert turn_ctx.user_turn_id == turn_repo.added[0].id
    assert len(turn_repo.added) == 2


