from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from src.core.llm_registry import ResolvedLLMRoute
from src.memory_system.clients.llm import LLMTextResult
from src.observability import LLMCallRequest, LLMFirstChunkTimeoutError, LLMGenerationHandle
from src.services import llm_gateway_service as llm_gateway_service_module
from src.services.llm_gateway_service import LLMGatewayService


class _PydanticRuntimeStub:
    def __init__(self) -> None:
        self.async_text_called = False
        self.async_json_called = False

    async def chat_async(self, system_prompt: str, user_message: str) -> str:
        self.async_text_called = True
        assert system_prompt
        assert user_message
        return "OK"

    async def chat_with_usage_async(
        self,
        system_prompt: str,
        user_message: str,
    ) -> LLMTextResult:
        self.async_text_called = True
        assert system_prompt
        assert user_message
        return LLMTextResult(
            text="OK",
            model="openai/gpt-4.1-mini",
            usage_details={"input": 13, "output": 2, "total": 15},
        )

    async def chat_json_async(
        self,
        system_prompt: str,
        user_message: str,
        default: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.async_json_called = True
        assert system_prompt
        assert user_message is not None
        return {
            "parsed_data": {"feedback_card": {"surface": "ok"}},
            "usage_details": {"input": 17, "output": 4, "total": 21},
        }

    def chat(self, system_prompt: str, user_message: str) -> str:  # pragma: no cover - guard
        raise AssertionError("sync chat() must not be used for pydantic_ai routes")

    def chat_json(  # pragma: no cover - guard
        self,
        system_prompt: str,
        user_message: str,
        default: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise AssertionError("sync chat_json() must not be used for pydantic_ai routes")

    async def chat_stream_messages_async(self, messages: list[dict[str, str]]):
        yield messages[0]["content"]


class _OpenAIRuntimeStub:
    def __init__(self) -> None:
        self.sync_text_called = False
        self.sync_json_called = False

    async def chat_async(self, system_prompt: str, user_message: str) -> str:  # pragma: no cover - guard
        raise AssertionError("async chat_async() must not be used for openai_sdk routes")

    async def chat_json_async(  # pragma: no cover - guard
        self,
        system_prompt: str,
        user_message: str,
        default: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise AssertionError("async chat_json_async() must not be used for openai_sdk routes")

    def chat(self, system_prompt: str, user_message: str, **kwargs) -> str:
        self.sync_text_called = True
        assert system_prompt
        assert user_message
        return "OK"

    def chat_with_usage(self, system_prompt: str, user_message: str, **kwargs) -> LLMTextResult:
        self.sync_text_called = True
        assert system_prompt
        assert user_message
        return LLMTextResult(
            text="OK",
            model="gpt-5.1-mini",
            usage_details={"input": 7, "output": 3, "total": 10},
        )

    def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        default: dict[str, object] | None = None,
        **kwargs,
    ) -> dict[str, object]:
        self.sync_json_called = True
        assert system_prompt
        assert user_message is not None
        return {"parsed_data": {"feedback_card": {"surface": "ok"}}}

    async def chat_stream_messages_async(self, messages: list[dict[str, str]]):
        yield messages[0]["content"]


class _SlowStreamRuntimeStub:
    async def chat_async(self, system_prompt: str, user_message: str) -> str:
        raise AssertionError("chat_async() not used")

    async def chat_json_async(
        self,
        system_prompt: str,
        user_message: str,
        default: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise AssertionError("chat_json_async() not used")

    def chat(self, system_prompt: str, user_message: str, **kwargs) -> str:
        raise AssertionError("chat() not used")

    def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        default: dict[str, object] | None = None,
        **kwargs,
    ) -> dict[str, object]:
        raise AssertionError("chat_json() not used")

    async def chat_stream_messages_async(self, messages: list[dict[str, str]]):
        await asyncio.sleep(0.05)
        yield "late"


class _SameTaskGuard:
    def __init__(self) -> None:
        self._entered_task: asyncio.Task[object] | None = None

    async def __aenter__(self) -> _SameTaskGuard:
        self._entered_task = asyncio.current_task()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if asyncio.current_task() is not self._entered_task:
            raise RuntimeError("Attempted to exit context in a different task")
        return False


class _GuardedStreamRuntimeStub:
    async def chat_async(self, system_prompt: str, user_message: str) -> str:
        raise AssertionError("chat_async() not used")

    async def chat_json_async(
        self,
        system_prompt: str,
        user_message: str,
        default: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise AssertionError("chat_json_async() not used")

    def chat(self, system_prompt: str, user_message: str, **kwargs) -> str:
        raise AssertionError("chat() not used")

    def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        default: dict[str, object] | None = None,
        **kwargs,
    ) -> dict[str, object]:
        raise AssertionError("chat_json() not used")

    async def chat_stream_messages_async(self, messages: list[dict[str, str]]):
        async with _SameTaskGuard():
            yield "first"
            yield "second"


class _UsageStreamRuntimeStub(_GuardedStreamRuntimeStub):
    def pop_last_stream_usage_details(self) -> dict[str, int]:
        return {"input": 11, "output": 5, "total": 16}

    def pop_last_stream_cost_details(self) -> None:
        return None


class _PydanticUsageStreamRuntimeStub(_GuardedStreamRuntimeStub):
    def pop_last_stream_usage_details(self) -> dict[str, int]:
        return {"input": 19, "output": 6, "total": 25}

    def pop_last_stream_cost_details(self) -> None:
        return None


class _SplitUsageStreamRuntimeStub(_GuardedStreamRuntimeStub):
    def __init__(self) -> None:
        self._usage: dict[str, int] = {}

    async def chat_stream_messages_async(self, messages: list[dict[str, str]]):
        self._usage.update({"input": 23, "cache_read_input_tokens": 7})
        yield "first"
        self._usage.update({"output": 8})
        yield "second"

    def pop_last_stream_usage_details(self) -> dict[str, int]:
        usage = dict(self._usage)
        usage["total"] = (
            usage.get("input", 0)
            + usage.get("output", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        return usage

    def pop_last_stream_cost_details(self) -> None:
        return None


def _build_route(*, sdk_kind: str) -> ResolvedLLMRoute:
    return ResolvedLLMRoute(
        provider="openrouter" if sdk_kind == "pydantic_ai" else "openai",
        model="openai/gpt-4.1-mini" if sdk_kind == "pydantic_ai" else "gpt-5.1-mini",
        sdk_kind=sdk_kind,
        api_key="test-key",
        api_base="https://example.com/v1",
        source="test",
        model_type_label="test:model",
    )


@pytest.mark.asyncio
async def test_generate_text_uses_async_runtime_for_pydantic_routes() -> None:
    service = LLMGatewayService()
    runtime = _PydanticRuntimeStub()
    service.get_runtime_client = lambda route, retry_policy="default": runtime  # type: ignore[method-assign]

    result = await service.generate_text(
        route=_build_route(sdk_kind="pydantic_ai"),
        system_prompt="Reply with exactly OK.",
        user_message="say ok",
        operation="test.text",
        route_role="test",
    )

    assert result == "OK"
    assert runtime.async_text_called is True




@pytest.mark.asyncio
async def test_generate_text_keeps_sync_runtime_for_openai_routes() -> None:
    service = LLMGatewayService()
    runtime = _OpenAIRuntimeStub()
    service.get_runtime_client = lambda route, retry_policy="default": runtime  # type: ignore[method-assign]

    result = await service.generate_text(
        route=_build_route(sdk_kind="openai_sdk"),
        system_prompt="Reply with exactly OK.",
        user_message="say ok",
        operation="test.text",
        route_role="test",
    )

    assert result == "OK"
    assert runtime.sync_text_called is True




@pytest.mark.asyncio
async def test_stream_text_preserves_same_task_context() -> None:
    service = LLMGatewayService()
    runtime = _GuardedStreamRuntimeStub()
    service.get_runtime_client = lambda route, retry_policy="default": runtime  # type: ignore[method-assign]

    chunks = [
        chunk
        async for chunk in service.stream_text(
            route=_build_route(sdk_kind="pydantic_ai"),
            messages=[{"role": "user", "content": "hello"}],
            operation="chat.reply.stream",
            route_role="chat",
        )
    ]

    assert chunks == ["first", "second"]




@pytest.mark.asyncio
async def test_stream_text_passes_split_stream_usage_to_langfuse_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LLMGatewayService()
    runtime = _SplitUsageStreamRuntimeStub()
    service.get_runtime_client = lambda route, retry_policy="default": runtime  # type: ignore[method-assign]
    captured_finish: dict[str, object] = {}

    def _capture_finish(
        handle,
        *,
        output,
        usage_details=None,
        cost_details=None,
    ) -> None:
        captured_finish["output"] = output
        captured_finish["usage_details"] = usage_details

    monkeypatch.setattr(llm_gateway_service_module, "finish_llm_generation", _capture_finish)

    chunks = [
        chunk
        async for chunk in service.stream_text(
            route=_build_route(sdk_kind="anthropic_sdk"),
            messages=[{"role": "user", "content": "hello"}],
            operation="chat.reply.stream",
            route_role="chat",
        )
    ]

    assert chunks == ["first", "second"]
    assert captured_finish["output"] == "firstsecond"
    assert captured_finish["usage_details"] == {
        "input": 23,
        "output": 8,
        "cache_read_input_tokens": 7,
        "total": 38,
    }


@pytest.mark.asyncio
async def test_stream_text_raises_first_chunk_timeout() -> None:
    service = LLMGatewayService()
    runtime = _SlowStreamRuntimeStub()
    service.get_runtime_client = lambda route, retry_policy="default": runtime  # type: ignore[method-assign]

    original_timeout = llm_gateway_service_module._FIRST_CHUNK_TIMEOUT_SECONDS
    llm_gateway_service_module._FIRST_CHUNK_TIMEOUT_SECONDS = 0.01
    with pytest.raises(LLMFirstChunkTimeoutError):
        try:
            async for _ in service.stream_text(
                route=_build_route(sdk_kind="pydantic_ai"),
                messages=[{"role": "user", "content": "hello"}],
                operation="chat.reply.stream",
                route_role="chat",
            ):
                pass
        finally:
            llm_gateway_service_module._FIRST_CHUNK_TIMEOUT_SECONDS = original_timeout


@pytest.mark.parametrize(
    ("method", "runtime_factory", "expected"),
    [
        ("generate_text", _PydanticRuntimeStub, "OK"),
        ("stream_text", _GuardedStreamRuntimeStub, ["first", "second"]),
    ],
    ids=["generate_text", "stream_text"],
)
@pytest.mark.asyncio
async def test_text_generation_runs_inside_observability_scope(
    monkeypatch: pytest.MonkeyPatch, method: str, runtime_factory, expected
) -> None:
    service = LLMGatewayService()
    runtime = runtime_factory()
    service.get_runtime_client = lambda route, retry_policy="default": runtime  # type: ignore[method-assign]

    state = {"scope_active": False, "observed": False}

    @contextmanager
    def _fake_scope(**kwargs):
        state["scope_active"] = True
        try:
            yield
        finally:
            state["scope_active"] = False

    def _fake_start(
        request: LLMCallRequest,
        *,
        manual: bool = False,
        trace_context: dict[str, str] | None = None,
    ) -> LLMGenerationHandle:
        state["observed"] = state["scope_active"]
        return LLMGenerationHandle(request=request, llm_call_id="call-1")

    monkeypatch.setattr(llm_gateway_service_module, "observability_scope", _fake_scope)
    monkeypatch.setattr(llm_gateway_service_module, "start_llm_generation", _fake_start)
    monkeypatch.setattr(
        llm_gateway_service_module,
        "finish_llm_generation",
        lambda handle, *, output, usage_details=None, cost_details=None: None,
    )

    route = _build_route(sdk_kind="pydantic_ai")
    if method == "generate_text":
        result = await service.generate_text(
            route=route,
            system_prompt="Reply with exactly OK.",
            user_message="say ok",
            operation="chat.reply.stream",
            route_role="chat",
            user_id="user-1",
            chat_id="chat-1",
        )
    else:
        result = [
            chunk
            async for chunk in service.stream_text(
                route=route,
                messages=[{"role": "user", "content": "hello"}],
                operation="chat.reply.stream",
                route_role="chat",
                user_id="user-1",
                chat_id="chat-1",
            )
        ]

    assert result == expected
    assert state["observed"] is True
