"""Unified LLM gateway with SDK routing by provider."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, Literal, Protocol

import anthropic
import openai
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from src.core.config import settings as app_settings
from src.core.llm_registry import ResolvedLLMRoute
from src.memory_system.clients.llm import LLMClient, LLMTextResult
from src.memory_system.exceptions import LLMAuthenticationError
from src.observability import (
    LLMCallRequest,
    LLMFirstChunkTimeoutError,
    LLMGenerationHandle,
    LLMRetryObserver,
    bind_llm_generation_context,
    build_chat_session_id,
    build_model_label,
    capture_context_snapshot,
    finish_llm_generation,
    mark_llm_stream_chunk,
    observability_scope,
    record_cancelled_generation,
    record_empty_generation,
    record_llm_failure,
    record_llm_retry,
    record_timeout_generation,
    start_llm_generation,
)

logger = logging.getLogger(__name__)
_FIRST_CHUNK_TIMEOUT_SECONDS = min(max(app_settings.LLM_TIMEOUT_SECONDS, 1.0), 20.0)


class LLMRuntimeClient(Protocol):
    def chat(self, system_prompt: str, user_message: str) -> str: ...

    def chat_with_usage(self, system_prompt: str, user_message: str) -> LLMTextResult: ...

    def chat_json(
        self, system_prompt: str, user_message: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def chat_async(self, system_prompt: str, user_message: str) -> str: ...

    async def chat_json_async(
        self, system_prompt: str, user_message: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def chat_stream_messages_async(
        self, messages: list[dict[str, Any]]
    ) -> AsyncGenerator[str, None]: ...


async def _close_async_iter_safely(stream_iter: AsyncIterator[str]) -> None:
    aclose = getattr(stream_iter, "aclose", None)
    if not callable(aclose):
        return
    try:
        await aclose()
    except Exception:
        logger.debug("Failed to close LLM async stream iterator cleanly", exc_info=True)


def _supports_retry_observer(callable_obj: Any) -> bool:
    try:
        return "retry_observer" in inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False


def _chat_session_or_none(chat_id: str | None) -> str | None:
    if not chat_id:
        return None
    return build_chat_session_id(chat_id)


def _run_coroutine_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("Synchronous LLM calls must not run inside an active event loop")


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _strip_response_wrappers(response: str) -> str:
    text = (response or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


def _extract_json_substring(text: str) -> str | None:
    start = -1
    opening = ""
    closing = ""
    for idx, ch in enumerate(text):
        if ch == "{":
            start = idx
            opening = "{"
            closing = "}"
            break
        if ch == "[":
            start = idx
            opening = "["
            closing = "]"
            break

    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opening:
            depth += 1
            continue
        if ch == closing:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def _safe_parse_json(response: str, default: dict[str, Any]) -> dict[str, Any]:
    if not response:
        logger.error("Empty response from LLM")
        return default

    text = _strip_response_wrappers(response)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        candidate = _extract_json_substring(text)
        if candidate:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        logger.error("Invalid JSON response: %s. Response: %s", exc, response[:200])
        return default


def _json_call_result(response_result: LLMTextResult, parsed_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "parsed_data": parsed_data,
        "raw_response": response_result.text,
        "model": response_result.model,
        "success": True,
        "usage_details": response_result.usage_details,
        "cost_details": response_result.cost_details,
    }


def _usage_details_from_json_result(result: dict[str, Any]) -> dict[str, Any] | None:
    usage_details = result.get("usage_details")
    return usage_details if isinstance(usage_details, dict) and usage_details else None


def _cost_details_from_json_result(result: dict[str, Any]) -> dict[str, Any] | None:
    cost_details = result.get("cost_details")
    return cost_details if isinstance(cost_details, dict) and cost_details else None


def _usage_details_from_response(response: Any) -> dict[str, Any] | None:
    return LLMClient._extract_usage_details(response)


def _cost_details_from_response(response: Any) -> dict[str, Any] | None:
    return LLMClient._extract_cost_details(response)


class _PydanticAIRuntimeClient:
    def __init__(self, route: ResolvedLLMRoute, *, retry_policy: RetryPolicy = "default"):
        self._route = route
        provider_kwargs: dict[str, Any] = {
            "base_url": route.api_base,
            "api_key": route.api_key,
        }
        if retry_policy == "none":
            # Force a single upstream attempt for evaluation calls.
            provider_kwargs["openai_client"] = openai.AsyncOpenAI(
                base_url=route.api_base,
                api_key=route.api_key or "",
                max_retries=0,
            )
        self._provider = OpenAIProvider(**provider_kwargs)
        self._model = OpenAIChatModel(route.model, provider=self._provider)
        self._agent = Agent(self._model)

    @staticmethod
    def _normalize_error(exc: Exception, route: ResolvedLLMRoute) -> Exception:
        if isinstance(exc, openai.AuthenticationError):
            detail = LLMClient._extract_auth_error_detail(exc)
            return LLMAuthenticationError(
                model=route.model,
                provider=route.provider,
                original_error=exc,
                detail=detail,
            )
        return exc

    def _build_history(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str | None, list[ModelRequest | ModelResponse] | None, str]:
        instructions_parts: list[str] = []
        conversation: list[tuple[str, str]] = []
        for message in messages:
            role = (message.get("role") or "").strip().lower()
            raw_content = message.get("content")
            if isinstance(raw_content, list):
                text_parts = [p["text"] for p in raw_content if isinstance(p, dict) and p.get("type") == "text"]
                content = "\n".join(text_parts)
            else:
                content = (raw_content or "").strip()
            if not content:
                continue
            if role == "system":
                instructions_parts.append(content)
                continue
            if role in {"user", "assistant"}:
                conversation.append((role, content))

        if not conversation:
            raise ValueError("messages must contain at least one user or assistant message")
        last_role, last_content = conversation[-1]
        if last_role != "user":
            raise ValueError("last message must be user for non-openai provider stream/text calls")

        history: list[ModelRequest | ModelResponse] = []
        for role, content in conversation[:-1]:
            if role == "user":
                history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
            else:
                history.append(
                    ModelResponse(
                        parts=[TextPart(content=content)],
                        model_name=self._route.model,
                        provider_name=self._route.provider,
                    )
                )

        instructions = "\n\n".join(part for part in instructions_parts if part.strip()) or None
        return instructions, history or None, last_content

    @staticmethod
    def _extract_model_response_text(response: ModelResponse) -> str:
        parts: list[str] = []
        for part in response.parts:
            if isinstance(part, TextPart) and part.content:
                parts.append(part.content)
        return "".join(parts).strip()

    async def _chat_async(self, system_prompt: str, user_message: str) -> LLMTextResult:
        try:
            response = await self._model.request(
                [ModelRequest(parts=[UserPromptPart(content=user_message)], instructions=system_prompt)],
                model_settings=None,
                model_request_parameters=ModelRequestParameters(
                    output_mode="text",
                    allow_text_output=True,
                ),
            )
        except Exception as exc:
            raise self._normalize_error(exc, self._route) from exc
        return LLMTextResult(
            text=self._extract_model_response_text(response),
            model=self._route.model,
            usage_details=_usage_details_from_response(response),
            cost_details=_cost_details_from_response(response),
        )

    async def chat_with_usage_async(self, system_prompt: str, user_message: str) -> LLMTextResult:
        return await self._chat_async(system_prompt, user_message)

    async def chat_async(self, system_prompt: str, user_message: str) -> str:
        return (await self.chat_with_usage_async(system_prompt, user_message)).text

    def chat(self, system_prompt: str, user_message: str) -> str:
        return _run_coroutine_sync(self.chat_with_usage_async(system_prompt, user_message)).text

    async def chat_json_async(
        self, system_prompt: str, user_message: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if default is None:
            default = {}
        response_result = await self.chat_with_usage_async(system_prompt, user_message)
        parsed_data = _safe_parse_json(response_result.text, default)
        return _json_call_result(response_result, parsed_data)

    def chat_json(
        self, system_prompt: str, user_message: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if default is None:
            default = {}
        response_result = _run_coroutine_sync(self.chat_with_usage_async(system_prompt, user_message))
        parsed_data = _safe_parse_json(response_result.text, default)
        return _json_call_result(response_result, parsed_data)

    async def chat_stream_messages_async(
        self, messages: list[dict[str, Any]]
    ) -> AsyncGenerator[str, None]:
        instructions, history, user_prompt = self._build_history(messages)
        try:
            async with self._agent.run_stream(
                user_prompt,
                message_history=history,
                instructions=instructions,
            ) as result:
                async for chunk in result.stream_text(delta=True, debounce_by=None):
                    text = _coerce_text(chunk)
                    if text:
                        yield text
                usage = result.usage()
                self._last_stream_usage_details = _usage_details_from_response(usage)
                self._last_stream_cost_details = _cost_details_from_response(usage)
        except Exception as exc:
            raise self._normalize_error(exc, self._route) from exc

    def pop_last_stream_usage_details(self) -> dict[str, Any] | None:
        usage_details = getattr(self, "_last_stream_usage_details", None)
        self._last_stream_usage_details = None
        return usage_details

    def pop_last_stream_cost_details(self) -> dict[str, Any] | None:
        cost_details = getattr(self, "_last_stream_cost_details", None)
        self._last_stream_cost_details = None
        return cost_details


class _AnthropicRuntimeClient:
    """Anthropic SDK client for MiniMax's Anthropic-compatible endpoint.

    Key behaviors:
    - streaming: use client.messages.create(..., stream=True), iterate raw events
    - streaming chunks: yield only text_delta; silently discard thinking_delta
    - content format: messages use Anthropic block format [{type:"text", text:"..."}]
    - system: passed as separate parameter
    - response_format json_object: NOT supported per MiniMax docs; text-parse fallback
    """

    def __init__(self, route: ResolvedLLMRoute, *, retry_policy: RetryPolicy = "default"):
        self._route = route
        # Anthropic SDK default is 2 retries; evaluation calls need one attempt.
        retry_kwargs: dict[str, Any] = {}
        if retry_policy == "none":
            retry_kwargs["max_retries"] = 0
        self._client = anthropic.Anthropic(
            api_key=route.api_key or "",
            base_url=route.api_base or "https://api.minimaxi.com/anthropic",
            timeout=app_settings.LLM_TIMEOUT_SECONDS,
            **retry_kwargs,
        )
        self._async_client = AsyncAnthropic(
            api_key=route.api_key or "",
            base_url=route.api_base or "https://api.minimaxi.com/anthropic",
            timeout=app_settings.LLM_TIMEOUT_SECONDS,
            **retry_kwargs,
        )
        self._model = route.model

    @staticmethod
    def _normalize_error(exc: Exception, route: ResolvedLLMRoute) -> Exception:
        if isinstance(exc, anthropic.AuthenticationError):
            return LLMAuthenticationError(
                model=route.model,
                provider=route.provider,
                original_error=exc,
                detail=getattr(exc, "message", str(exc)),
            )
        return exc

    def _extract_content_text(self, response: Any) -> str:
        """Extract text from message.content blocks, filtering out thinking blocks."""
        parts: list[str] = []
        for block in response.content:
            if block.type == "text":
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
        return "\n".join(parts)

    def _to_text_result(self, response: Any) -> LLMTextResult:
        return LLMTextResult(
            text=self._extract_content_text(response),
            model=self._model,
            usage_details=_usage_details_from_response(response),
            cost_details=_cost_details_from_response(response),
        )

    def _merge_stream_usage(self, response_or_usage: Any) -> None:
        usage_details = _usage_details_from_response(response_or_usage)
        if not usage_details:
            return

        current = getattr(self, "_last_stream_usage_details", None)
        merged = dict(current) if isinstance(current, dict) else {}
        explicit_total = usage_details.get("total")
        for key, value in usage_details.items():
            if key == "total":
                continue
            merged[key] = value

        recomputed_total = sum(
            int(merged.get(key, 0) or 0)
            for key in (
                "input",
                "output",
                "cache_read_input_tokens",
                "cache_write_input_tokens",
            )
        )
        if recomputed_total > 0:
            merged["total"] = recomputed_total
        elif isinstance(explicit_total, int):
            merged["total"] = explicit_total

        self._last_stream_usage_details = merged or None
        cost_details = _cost_details_from_response(response_or_usage)
        if cost_details:
            self._last_stream_cost_details = cost_details

    def _build_anthropic_messages(
        self, system_prompt: str, user_message: str
    ) -> tuple[str | None, list[dict]]:
        system = system_prompt.strip() or None
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": user_message}],
            }
        ]
        return system, messages

    def _convert_messages_for_stream(self, messages: list[dict[str, Any]]) -> tuple[str | None, list[dict]]:
        system = None
        anthropic_messages: list[dict] = []
        for msg in messages:
            role = (msg.get("role") or "").strip().lower()
            raw_content = msg.get("content")
            if not raw_content:
                continue
            if isinstance(raw_content, list):
                anthropic_parts = []
                for part in raw_content:
                    if part.get("type") == "text":
                        anthropic_parts.append({"type": "text", "text": part["text"]})
                    elif part.get("type") == "image_url":
                        url = part["image_url"]["url"]
                        if url.startswith("data:"):
                            media_type, _, data = url.partition(";base64,")
                            media_type = media_type.replace("data:", "")
                            anthropic_parts.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
                        else:
                            anthropic_parts.append({"type": "image", "source": {"type": "url", "url": url}})
                if role == "system":
                    text_parts = [p["text"] for p in anthropic_parts if p.get("type") == "text"]
                    system = "\n".join(text_parts)
                elif anthropic_parts:
                    anthropic_messages.append({"role": role, "content": anthropic_parts})
            else:
                content = raw_content.strip() if isinstance(raw_content, str) else str(raw_content)
                if not content:
                    continue
                if role == "system":
                    system = content
                else:
                    anthropic_messages.append({"role": role, "content": [{"type": "text", "text": content}]})
        return system, anthropic_messages

    # ---- sync text ----

    def chat(self, system_prompt: str, user_message: str) -> str:
        return self.chat_with_usage(system_prompt, user_message).text

    def chat_with_usage(self, system_prompt: str, user_message: str) -> LLMTextResult:
        try:
            system, messages = self._build_anthropic_messages(system_prompt, user_message)
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=system,
                messages=messages,
            )
            return self._to_text_result(response)
        except Exception as exc:
            raise self._normalize_error(exc, self._route) from exc

    # ---- sync JSON ----

    def chat_json(
        self, system_prompt: str, user_message: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if default is None:
            default = {}
        try:
            system, messages = self._build_anthropic_messages(system_prompt, user_message)
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=system,
                messages=messages,
            )
            response_result = self._to_text_result(response)
            parsed_data = _safe_parse_json(response_result.text, default)
            return _json_call_result(response_result, parsed_data)
        except Exception as exc:
            raise self._normalize_error(exc, self._route) from exc

    # ---- async text ----

    async def chat_async(self, system_prompt: str, user_message: str) -> str:
        return (await self.chat_with_usage_async(system_prompt, user_message)).text

    async def chat_with_usage_async(
        self, system_prompt: str, user_message: str
    ) -> LLMTextResult:
        system, messages = self._build_anthropic_messages(system_prompt, user_message)
        try:
            response = await self._async_client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=system,
                messages=messages,
            )
            return self._to_text_result(response)
        except Exception as exc:
            raise self._normalize_error(exc, self._route) from exc

    # ---- async JSON ----

    async def chat_json_async(
        self, system_prompt: str, user_message: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if default is None:
            default = {}
        system, messages = self._build_anthropic_messages(system_prompt, user_message)
        try:
            response = await self._async_client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=system,
                messages=messages,
            )
            response_result = self._to_text_result(response)
            parsed_data = _safe_parse_json(response_result.text, default)
            return _json_call_result(response_result, parsed_data)
        except Exception as exc:
            raise self._normalize_error(exc, self._route) from exc

    # ---- async streaming ----

    async def chat_stream_messages_async(
        self, messages: list[dict[str, Any]]
    ) -> AsyncGenerator[str, None]:
        system, anthropic_messages = self._convert_messages_for_stream(messages)
        if not anthropic_messages:
            return

        queue: asyncio.Queue[str | BaseException | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        snapshot = capture_context_snapshot()

        def _worker() -> None:
            try:
                stream = self._client.messages.create(
                    model=self._model,
                    max_tokens=4096,
                    system=system,
                    messages=anthropic_messages,
                    stream=True,
                )
                for event in stream:
                    if event.type == "message_start":
                        self._merge_stream_usage(getattr(event, "message", None))
                    elif event.type == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is None:
                            continue
                        if delta.type == "text_delta":
                            text = getattr(delta, "text", None)
                            if text:
                                loop.call_soon_threadsafe(queue.put_nowait, text)
                        # thinking_delta → silently discard
                    elif event.type == "message_delta":
                        self._merge_stream_usage(getattr(event, "usage", None))
                    elif event.type == "message_stop":
                        loop.call_soon_threadsafe(queue.put_nowait, None)
                        break
            except BaseException as exc:
                loop.call_soon_threadsafe(queue.put_nowait, self._normalize_error(exc, self._route))
                loop.call_soon_threadsafe(queue.put_nowait, None)

        thread = threading.Thread(
            target=lambda: snapshot.run(_worker),
            name="anthropic-stream-worker",
            daemon=True,
        )
        thread.start()

        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield item

    def pop_last_stream_usage_details(self) -> dict[str, Any] | None:
        usage_details = getattr(self, "_last_stream_usage_details", None)
        self._last_stream_usage_details = None
        return usage_details

    def pop_last_stream_cost_details(self) -> dict[str, Any] | None:
        cost_details = getattr(self, "_last_stream_cost_details", None)
        self._last_stream_cost_details = None
        return cost_details


RetryPolicy = Literal["default", "none"]


class LLMGatewayService:
    """Centralized route-aware SDK gateway."""

    def __init__(self):
        self._clients: dict[tuple[str, str, str, str, str], LLMRuntimeClient] = {}

    @staticmethod
    def _cache_key(route: ResolvedLLMRoute, retry_policy: RetryPolicy) -> tuple[str, str, str, str, str]:
        return (
            route.provider,
            route.model,
            route.api_base or "",
            route.api_key or "",
            retry_policy,
        )

    def get_runtime_client(
        self, route: ResolvedLLMRoute, *, retry_policy: RetryPolicy = "default"
    ) -> LLMRuntimeClient:
        key = self._cache_key(route, retry_policy)
        client = self._clients.get(key)
        if client is not None:
            return client

        if route.sdk_kind == "openai_sdk":
            # RetryExecutor treats max_retries as total attempts; evaluation
            # calls need exactly one upstream attempt, never zero.
            max_retries = 1 if retry_policy == "none" else app_settings.LLM_MAX_RETRIES
            client = LLMClient(
                primary_model=route.model,
                primary_api_key=route.api_key,
                primary_api_base=route.api_base,
                fallback_model=None,
                fallback_api_key=None,
                fallback_api_base=None,
                max_retries=max_retries,
                base_delay=app_settings.LLM_RETRY_BASE_DELAY_SECONDS,
                timeout_seconds=app_settings.LLM_TIMEOUT_SECONDS,
                primary_extra_headers=route.extra_headers,
                primary_extra_body=route.extra_body,
            )
        elif route.sdk_kind == "anthropic_sdk":
            client = _AnthropicRuntimeClient(route, retry_policy=retry_policy)
        else:
            client = _PydanticAIRuntimeClient(route, retry_policy=retry_policy)

        self._clients[key] = client
        return client

    @staticmethod
    def _build_call_request(
        *,
        route: ResolvedLLMRoute,
        operation: str,
        route_role: str,
        input_payload: Any,
        user_id: str | None,
        chat_id: str | None,
        turn_id: str | None,
        candidate_id: str | None,
        character_id: str | None,
        metadata: dict[str, Any] | None = None,
        capture_input: bool = True,
        capture_output: bool = True,
    ) -> LLMCallRequest:
        return LLMCallRequest(
            operation=operation,
            route_role=route_role,
            provider=route.provider,
            model=route.model,
            sdk_kind=route.sdk_kind,
            input_payload=input_payload,
            user_id=user_id,
            session_id=_chat_session_or_none(chat_id),
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
            metadata=metadata or {},
            tags=(route_role, operation),
            capture_input=capture_input,
            capture_output=capture_output,
        )

    @staticmethod
    def _retry_observer_for(handle: LLMGenerationHandle) -> LLMRetryObserver:
        return lambda event: record_llm_retry(handle, event)

    async def stream_text(
        self,
        *,
        route: ResolvedLLMRoute,
        messages: list[dict[str, Any]],
        operation: str,
        route_role: str,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
        retry_policy: RetryPolicy = "default",
        capture_input: bool = True,
        capture_output: bool = True,
    ) -> AsyncGenerator[str, None]:
        client = self.get_runtime_client(route, retry_policy=retry_policy)
        request = self._build_call_request(
            route=route,
            operation=operation,
            route_role=route_role,
            input_payload=messages,
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
            capture_input=capture_input,
            capture_output=capture_output,
        )

        with observability_scope(
            user_id=request.user_id,
            session_id=request.session_id,
            tags=list(request.tags),
            metadata=request.metadata,
        ):
            handle = start_llm_generation(request, manual=True, trace_context=trace_context)
            retry_observer = self._retry_observer_for(handle)
            with bind_llm_generation_context(handle):
                if _supports_retry_observer(client.chat_stream_messages_async):
                    stream = client.chat_stream_messages_async(
                        messages,
                        retry_observer=retry_observer,
                    )
                else:
                    stream = client.chat_stream_messages_async(messages)
                stream_iter = stream.__aiter__()
                chunks: list[str] = []
                terminal_error_recorded = False
                try:
                    try:
                        async with asyncio.timeout(_FIRST_CHUNK_TIMEOUT_SECONDS):
                            first_chunk = await anext(stream_iter)
                    except StopAsyncIteration:
                        record_empty_generation(handle)
                        return
                    except TimeoutError as exc:
                        await _close_async_iter_safely(stream_iter)
                        record_timeout_generation(handle, _FIRST_CHUNK_TIMEOUT_SECONDS)
                        terminal_error_recorded = True
                        raise LLMFirstChunkTimeoutError(
                            operation=operation,
                            model=build_model_label(provider=route.provider, model=route.model),
                            timeout_s=_FIRST_CHUNK_TIMEOUT_SECONDS,
                        ) from exc
                    except asyncio.CancelledError:
                        await _close_async_iter_safely(stream_iter)
                        record_cancelled_generation(handle)
                        terminal_error_recorded = True
                        raise
                    except Exception as exc:
                        await _close_async_iter_safely(stream_iter)
                        record_llm_failure(handle, exc)
                        terminal_error_recorded = True
                        raise

                    mark_llm_stream_chunk(handle, first_chunk)
                    chunks.append(first_chunk)
                    yield first_chunk

                    async for chunk in stream_iter:
                        mark_llm_stream_chunk(handle, chunk)
                        chunks.append(chunk)
                        yield chunk

                    pop_usage = getattr(client, "pop_last_stream_usage_details", None)
                    pop_cost = getattr(client, "pop_last_stream_cost_details", None)
                    usage_details = pop_usage() if callable(pop_usage) else None
                    cost_details = pop_cost() if callable(pop_cost) else None
                    finish_llm_generation(
                        handle,
                        output="".join(chunks),
                        usage_details=usage_details,
                        cost_details=cost_details,
                    )
                except asyncio.CancelledError:
                    if not terminal_error_recorded:
                        record_cancelled_generation(handle)
                    raise
                except Exception as exc:
                    if not terminal_error_recorded:
                        record_llm_failure(handle, exc)
                    raise

    async def stream_turn_with_tools(
        self,
        *,
        route: ResolvedLLMRoute,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        operation: str,
        route_role: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        first_chunk_timeout_s: float = _FIRST_CHUNK_TIMEOUT_SECONDS,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        character_id: str | None = None,
        should_interrupt: Callable[[], bool] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream one tool-aware assistant turn with the shared observability wrapper.

        Emits ``{"type": "text"}`` and ``{"type": "tool_calls"}`` events; the
        consumer owns tool execution and the loop.

        ``should_interrupt`` is polled after each text/reasoning delta. Returning
        ``True`` closes the upstream stream without emitting tool calls, which
        lets a caller preempt a text-only generation (for example when the user
        sends a guide message). Tool-call events are never cut off: when the
        upstream has already produced them, they are yielded and the consumer
        finishes the tool batch before acting on the interruption signal.
        """
        client = self.get_runtime_client(route, retry_policy="default")
        request = self._build_call_request(
            route=route,
            operation=operation,
            route_role=route_role,
            input_payload={
                "messages": messages,
                "tools": [t.get("function", {}).get("name") for t in tools],
            },
            user_id=user_id,
            chat_id=chat_id,
            turn_id=None,
            candidate_id=None,
            character_id=character_id,
        )

        with observability_scope(
            user_id=request.user_id,
            session_id=request.session_id,
            tags=list(request.tags),
            metadata=request.metadata,
        ):
            handle = start_llm_generation(request, manual=True, trace_context=trace_context)
            with bind_llm_generation_context(handle):
                stream = client.stream_chat_with_tools_async(
                    messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    extra_body_override=(
                        {"reasoning_effort": reasoning_effort} if reasoning_effort else None
                    ),
                )
                stream_iter = stream.__aiter__()
                texts: list[str] = []
                terminal_error_recorded = False
                try:
                    try:
                        async with asyncio.timeout(first_chunk_timeout_s):
                            first = await anext(stream_iter)
                    except StopAsyncIteration:
                        record_empty_generation(handle)
                        return
                    except TimeoutError as exc:
                        await _close_async_iter_safely(stream_iter)
                        record_timeout_generation(handle, first_chunk_timeout_s)
                        terminal_error_recorded = True
                        raise LLMFirstChunkTimeoutError(
                            operation=operation,
                            model=build_model_label(provider=route.provider, model=route.model),
                            timeout_s=first_chunk_timeout_s,
                        ) from exc
                    except asyncio.CancelledError:
                        await _close_async_iter_safely(stream_iter)
                        record_cancelled_generation(handle)
                        terminal_error_recorded = True
                        raise
                    except Exception as exc:
                        await _close_async_iter_safely(stream_iter)
                        record_llm_failure(handle, exc)
                        terminal_error_recorded = True
                        raise

                    def interrupted(event: dict[str, Any]) -> bool:
                        return (
                            should_interrupt is not None
                            and event.get("type") in ("text", "reasoning")
                            and should_interrupt()
                        )

                    if first.get("type") == "text":
                        mark_llm_stream_chunk(handle, first["text"])
                        texts.append(first["text"])
                    yield first
                    if interrupted(first):
                        await _close_async_iter_safely(stream_iter)
                        record_cancelled_generation(handle)
                        return

                    async for event in stream_iter:
                        if event.get("type") == "text":
                            mark_llm_stream_chunk(handle, event["text"])
                            texts.append(event["text"])
                        yield event
                        if interrupted(event):
                            await _close_async_iter_safely(stream_iter)
                            record_cancelled_generation(handle)
                            return

                    pop_usage = getattr(client, "pop_last_stream_usage_details", None)
                    pop_cost = getattr(client, "pop_last_stream_cost_details", None)
                    finish_llm_generation(
                        handle,
                        output="".join(texts),
                        usage_details=pop_usage() if callable(pop_usage) else None,
                        cost_details=pop_cost() if callable(pop_cost) else None,
                    )
                except asyncio.CancelledError:
                    if not terminal_error_recorded:
                        record_cancelled_generation(handle)
                    raise
                except Exception as exc:
                    if not terminal_error_recorded:
                        record_llm_failure(handle, exc)
                    raise

    async def generate_text(
        self,
        *,
        route: ResolvedLLMRoute,
        system_prompt: str,
        user_message: str,
        operation: str,
        route_role: str,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
        retry_policy: RetryPolicy = "default",
        capture_input: bool = True,
        capture_output: bool = True,
    ) -> str:
        client = self.get_runtime_client(route, retry_policy=retry_policy)
        request = self._build_call_request(
            route=route,
            operation=operation,
            route_role=route_role,
            input_payload=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
            capture_input=capture_input,
            capture_output=capture_output,
        )

        with observability_scope(
            user_id=request.user_id,
            session_id=request.session_id,
            tags=list(request.tags),
            metadata=request.metadata,
        ):
            handle = start_llm_generation(request, manual=True, trace_context=trace_context)
            retry_observer = self._retry_observer_for(handle)
            with bind_llm_generation_context(handle):
                try:
                    async_chat_with_usage = getattr(client, "chat_with_usage_async", None)
                    if callable(async_chat_with_usage):
                        response_result = await async_chat_with_usage(system_prompt, user_message)
                        result = response_result.text
                        usage_details = response_result.usage_details
                        cost_details = response_result.cost_details
                    elif route.sdk_kind != "openai_sdk":
                        result = await client.chat_async(system_prompt, user_message)
                        usage_details = None
                        cost_details = None
                    elif hasattr(client, "chat_with_usage"):
                        response_result = await asyncio.to_thread(
                            client.chat_with_usage,
                            system_prompt,
                            user_message,
                            retry_observer=retry_observer,
                        )
                        result = response_result.text
                        usage_details = response_result.usage_details
                        cost_details = response_result.cost_details
                    elif _supports_retry_observer(client.chat):
                        result = await asyncio.to_thread(
                            client.chat,
                            system_prompt,
                            user_message,
                            retry_observer=retry_observer,
                        )
                        usage_details = None
                        cost_details = None
                    else:
                        result = await asyncio.to_thread(client.chat, system_prompt, user_message)
                        usage_details = None
                        cost_details = None
                    finish_llm_generation(
                        handle,
                        output=result,
                        usage_details=usage_details,
                        cost_details=cost_details,
                    )
                    return result
                except asyncio.CancelledError:
                    record_cancelled_generation(handle)
                    raise
                except Exception as exc:
                    record_llm_failure(handle, exc)
                    raise

    async def generate_json(
        self,
        *,
        route: ResolvedLLMRoute,
        system_prompt: str,
        user_message: str,
        default: dict[str, Any],
        operation: str,
        route_role: str,
        trace_context: dict[str, str] | None = None,
        user_id: str | None = None,
        chat_id: str | None = None,
        turn_id: str | None = None,
        candidate_id: str | None = None,
        character_id: str | None = None,
        retry_policy: RetryPolicy = "default",
        capture_input: bool = True,
        capture_output: bool = True,
    ) -> dict[str, Any]:
        client = self.get_runtime_client(route, retry_policy=retry_policy)
        request = self._build_call_request(
            route=route,
            operation=operation,
            route_role=route_role,
            input_payload=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            user_id=user_id,
            chat_id=chat_id,
            turn_id=turn_id,
            candidate_id=candidate_id,
            character_id=character_id,
            capture_input=capture_input,
            capture_output=capture_output,
        )

        with observability_scope(
            user_id=request.user_id,
            session_id=request.session_id,
            tags=list(request.tags),
            metadata=request.metadata,
        ):
            handle = start_llm_generation(request, manual=True, trace_context=trace_context)
            retry_observer = self._retry_observer_for(handle)
            with bind_llm_generation_context(handle):
                try:
                    if route.sdk_kind != "openai_sdk":
                        raw_result = await client.chat_json_async(system_prompt, user_message, default)
                    elif _supports_retry_observer(client.chat_json):
                        raw_result = await asyncio.to_thread(
                            client.chat_json,
                            system_prompt,
                            user_message,
                            default,
                            retry_observer=retry_observer,
                        )
                    else:
                        raw_result = await asyncio.to_thread(
                            client.chat_json,
                            system_prompt,
                            user_message,
                            default,
                        )
                    parsed = raw_result.get("parsed_data")
                    result = parsed if isinstance(parsed, dict) else default
                    finish_llm_generation(
                        handle,
                        output=result,
                        usage_details=_usage_details_from_json_result(raw_result),
                        cost_details=_cost_details_from_json_result(raw_result),
                    )
                    return result
                except asyncio.CancelledError:
                    record_cancelled_generation(handle)
                    raise
                except Exception as exc:
                    record_llm_failure(handle, exc)
                    raise


__all__ = ["LLMGatewayService", "LLMRuntimeClient"]
