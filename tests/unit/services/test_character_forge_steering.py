"""Turn-steering semantics for the character-forge agent loop."""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from src.core.llm_registry import llm_registry
from src.db.models import CharacterForgeSession
from src.services.character_forge_service import (
    _SESSION_RUNTIMES,
    CharacterForgeService,
)
from src.services.character_forge_tools import ToolOutcome


@dataclass
class _FakeMessage:
    id: uuid.UUID
    seq: int
    role: str
    content: str | None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    ui_payloads: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] | None = None
    created_at: datetime | None = None


class _FakeRepo:
    def __init__(self, session: CharacterForgeSession):
        self.session = session
        self.messages: list[_FakeMessage] = []

    async def get_session(self, session_id: uuid.UUID, *, user_id: uuid.UUID):
        if session_id != self.session.id or user_id != self.session.user_id:
            return None
        return self.session

    async def list_messages(self, session_id: uuid.UUID, *, limit: int = 200):
        return list(self.messages)

    async def next_seq(self, session_id: uuid.UUID) -> int:
        return len(self.messages) + 1

    async def add_message(self, *, session_id: uuid.UUID, seq: int, role: str, **kwargs):
        message = _FakeMessage(
            id=uuid.uuid4(),
            seq=seq,
            role=role,
            content=kwargs.get("content"),
            tool_calls=kwargs.get("tool_calls"),
            tool_call_id=kwargs.get("tool_call_id"),
            tool_name=kwargs.get("tool_name"),
            ui_payloads=kwargs.get("ui_payloads") or [],
        )
        self.messages.append(message)
        return message

    async def update_message(self, message: _FakeMessage, **kwargs):
        if kwargs.get("ui_payloads") is not None:
            message.ui_payloads = kwargs["ui_payloads"]
        if kwargs.get("content") is not None:
            message.content = kwargs["content"]
        return message

    async def touch_session(self, session: CharacterForgeSession, **kwargs):
        for key, value in kwargs.items():
            setattr(session, key, value)
        return session

    async def characters_by_ids(self, character_ids: list[uuid.UUID]):
        return {}


class _FakeTools:
    def __init__(self):
        self.on_execute = None

    async def execute(self, name: str, arguments: dict[str, Any], context: Any):
        if self.on_execute is not None:
            await self.on_execute()
        return ToolOutcome(llm_result={"ok": True, "results": []})


class _FakeGateway:
    def __init__(
        self,
        script: list[list[dict[str, Any]]],
        *,
        interruptible: bool = True,
    ):
        self._script = script
        self._interruptible = interruptible
        self.calls: list[list[dict[str, Any]]] = []

    async def stream_turn_with_tools(
        self,
        *,
        messages: list[dict[str, Any]],
        should_interrupt=None,
        **kwargs,
    ):
        self.calls.append(copy.deepcopy(messages))
        for event in self._script[len(self.calls) - 1]:
            yield event
            if (
                self._interruptible
                and should_interrupt is not None
                and event.get("type") in ("text", "reasoning")
                and should_interrupt()
            ):
                return


def _build(script: list[list[dict[str, Any]]], *, interruptible: bool = True):
    session = CharacterForgeSession(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="新角色",
    )
    repo = _FakeRepo(session)
    gateway = _FakeGateway(script, interruptible=interruptible)
    tools = _FakeTools()
    service = CharacterForgeService(
        repo=repo,
        tool_executor=tools,
        llm_gateway_service=gateway,
        tts_gateway_service=SimpleNamespace(),
        search_repository=SimpleNamespace(),
        character_service=SimpleNamespace(),
        chat_service=SimpleNamespace(),
    )
    user = SimpleNamespace(id=session.user_id)
    return service, repo, gateway, user, session, tools


def _parse(chunk: str) -> dict[str, Any]:
    assert chunk.startswith("data: ")
    return json.loads(chunk[len("data: ") :])


@pytest.fixture(autouse=True)
def _clear_runtimes():
    _SESSION_RUNTIMES.clear()
    yield
    _SESSION_RUNTIMES.clear()


async def test_steer_is_injected_after_tool_results(monkeypatch):
    monkeypatch.setattr(
        llm_registry,
        "resolve_chat_route",
        lambda: SimpleNamespace(api_key="test", provider="test", model="test"),
    )
    service, repo, gateway, user, session, tools = _build(
        [
            [
                {"type": "text", "text": "我先查一下。"},
                {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "call_1",
                            "name": "search_web",
                            "arguments": '{"query":"伦敦 古籍"}',
                        }
                    ],
                },
            ],
            [{"type": "text", "text": "收到补充，继续。"}],
        ]
    )

    queued: dict[str, Any] = {}

    async def queue_steer() -> None:
        queued["result"] = await service.steer(
            user,
            session.id,
            content="再加一点书卷气",
            steer_id="steer-1",
        )

    # The guide arrives while the tool is running, so it must wait for the
    # whole tool batch to finish.
    tools.on_execute = queue_steer

    events = [
        _parse(chunk)
        async for chunk in service.stream_reply(
            user, session.id, "创建一个修古籍的角色"
        )
    ]
    assert queued["result"].queue_length == 1
    drained = [event for event in events if event["type"] == "steer_drained"]
    assert len(drained) == 1
    assert drained[0]["items"][0]["steer_id"] == "steer-1"
    assert events[-1]["type"] == "done"

    # The second model request must see: assistant(tool calls), tool result,
    # then the steering user message. A user row may never sit between the
    # assistant tool calls and their results.
    second = gateway.calls[1]
    assert [message["role"] for message in second[-3:]] == [
        "assistant",
        "tool",
        "user",
    ]
    assert second[-1]["content"] == "再加一点书卷气"

    assert [message.role for message in repo.messages] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert repo.messages[3].content == "再加一点书卷气"


async def test_steer_interrupts_a_text_only_generation(monkeypatch):
    monkeypatch.setattr(
        llm_registry,
        "resolve_chat_route",
        lambda: SimpleNamespace(api_key="test", provider="test", model="test"),
    )
    service, repo, gateway, user, session, _tools = _build(
        [
            [
                {"type": "text", "text": "第一段。"},
                {"type": "reasoning", "text": "还在继续想。"},
                {"type": "text", "text": "这段不该出现。"},
            ],
            [{"type": "text", "text": "按新要求重来。"}],
        ]
    )

    stream = service.stream_reply(user, session.id, "写一段很长的回答")
    assert _parse(await anext(stream))["type"] == "meta"
    assert _parse(await anext(stream))["content"] == "第一段。"

    await service.steer(
        user,
        session.id,
        content="先停一下，换个方向",
        steer_id="steer-3",
    )

    events = [_parse(chunk) async for chunk in stream]
    output = "".join(
        event["content"] for event in events if event["type"] == "chunk"
    )
    assert output == "按新要求重来。"
    assert "这段不该出现。" not in output
    assert any(event["type"] == "steer_drained" for event in events)

    # The interrupted partial answer becomes history before the guide, and the
    # follow-up request is issued without waiting for the original response.
    second = gateway.calls[1]
    assert [message["role"] for message in second[-2:]] == ["assistant", "user"]
    assert second[-2]["content"] == "第一段。"
    assert second[-1]["content"] == "先停一下，换个方向"
    assert [message.role for message in repo.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]


async def test_no_tool_reply_continues_when_steer_is_pending(monkeypatch):
    monkeypatch.setattr(
        llm_registry,
        "resolve_chat_route",
        lambda: SimpleNamespace(api_key="test", provider="test", model="test"),
    )
    service, repo, gateway, user, session, _tools = _build(
        [
            [{"type": "text", "text": "第一版方案是这样。"}],
            [{"type": "text", "text": "按你的补充调整好了。"}],
        ],
        # Simulate a guide that lands after the upstream stream has already
        # produced its last delta: the turn must still continue.
        interruptible=False,
    )

    stream = service.stream_reply(user, session.id, "先给个角色方案")
    meta = _parse(await anext(stream))
    assert meta["type"] == "meta"
    first = _parse(await anext(stream))
    assert first["content"] == "第一版方案是这样。"

    await service.steer(
        user,
        session.id,
        content="把年龄改成四十岁",
        steer_id="steer-2",
    )

    events = [_parse(chunk) async for chunk in stream]
    kinds = [event["type"] for event in events]
    assert "steer_drained" in kinds
    assert kinds.index("steer_drained") < kinds.index("chunk")
    assert kinds[-1] == "done"

    second = gateway.calls[1]
    assert [message["role"] for message in second[-2:]] == ["assistant", "user"]
    assert second[-2]["content"] == "第一版方案是这样。"
    assert second[-1]["content"] == "把年龄改成四十岁"

    assert [message.role for message in repo.messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
