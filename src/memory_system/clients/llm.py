"""LLM client backed by the OpenAI Python SDK."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import openai
from openai import AsyncOpenAI, OpenAI

from src.observability import LLMRetryObserver

from ..exceptions import LLMAuthenticationError, LLMCallError
from ..utils.retry import RetryExecutor

logger = logging.getLogger(__name__)

_RETRYABLE_OPENAI_EXCEPTIONS = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    openai.RateLimitError,
)


@dataclass(frozen=True)
class _ModelRoute:
    model: str
    api_key: str | None = None
    base_url: str | None = None
    sync_client: OpenAI | None = None
    async_client: AsyncOpenAI | None = None
    extra_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class LLMTextResult:
    text: str
    model: str
    usage_details: dict[str, Any] | None = None
    cost_details: dict[str, Any] | None = None


class LLMClient:
    """LLM model client using the OpenAI-compatible Python SDK."""

    @staticmethod
    def _normalize_model_name(model: str, base_url: str | None) -> str:
        normalized_model = (model or "").strip()
        if not normalized_model:
            return normalized_model

        normalized_base = (base_url or "").strip().lower()
        if (
            normalized_base
            and normalized_model.startswith("deepseek/")
            and "deepseek.com" in normalized_base
        ):
            return normalized_model.split("/", 1)[1]
        if (
            normalized_base
            and normalized_model.startswith("openrouter/")
            and "openrouter.ai" in normalized_base
        ):
            return normalized_model.split("/", 1)[1]
        if (
            normalized_base
            and normalized_model.startswith("dashscope/")
            and "dashscope.aliyuncs.com" in normalized_base
        ):
            return normalized_model.split("/", 1)[1]
        return normalized_model

    @staticmethod
    def _build_sync_client(
        api_key: str | None,
        base_url: str | None,
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
    ) -> OpenAI:
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            default_headers=extra_headers,
        )

    @staticmethod
    def _build_async_client(
        api_key: str | None,
        base_url: str | None,
        timeout_seconds: float,
        extra_headers: dict[str, str] | None = None,
    ) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
            default_headers=extra_headers,
        )

    def __init__(
        self,
        primary_model: str,
        primary_api_key: str | None = None,
        primary_api_base: str | None = None,
        fallback_model: str | None = None,
        fallback_api_key: str | None = None,
        fallback_api_base: str | None = None,
        max_retries: int = 3,
        base_delay: float = 1.0,
        timeout_seconds: float = 60.0,
        primary_extra_headers: dict[str, str] | None = None,
        primary_extra_body: dict[str, Any] | None = None,
        fallback_extra_headers: dict[str, str] | None = None,
        fallback_extra_body: dict[str, Any] | None = None,
    ):
        """Initialize SDK-backed client."""
        if not (primary_model or "").strip():
            raise ValueError("primary_model is required")

        normalized_primary_model = self._normalize_model_name(primary_model, primary_api_base)
        self._routes: list[_ModelRoute] = [
            _ModelRoute(
                model=normalized_primary_model,
                api_key=(primary_api_key or "").strip() or None,
                base_url=(primary_api_base or "").strip() or None,
                sync_client=self._build_sync_client(
                    primary_api_key,
                    primary_api_base,
                    timeout_seconds,
                    primary_extra_headers,
                ),
                async_client=self._build_async_client(
                    primary_api_key,
                    primary_api_base,
                    timeout_seconds,
                    primary_extra_headers,
                ),
                extra_body=primary_extra_body,
            )
        ]
        if (fallback_model or "").strip():
            normalized_fallback_model = self._normalize_model_name(
                fallback_model, fallback_api_base
            )
            self._routes.append(
                _ModelRoute(
                    model=normalized_fallback_model,
                    api_key=(fallback_api_key or "").strip() or None,
                    base_url=(fallback_api_base or "").strip() or None,
                    sync_client=self._build_sync_client(
                        fallback_api_key,
                        fallback_api_base,
                        timeout_seconds,
                        fallback_extra_headers,
                    ),
                    async_client=self._build_async_client(
                        fallback_api_key,
                        fallback_api_base,
                        timeout_seconds,
                        fallback_extra_headers,
                    ),
                    extra_body=fallback_extra_body,
                )
            )

        self._model_label = " -> ".join(route.model for route in self._routes)
        self._max_retries = max_retries
        self._base_delay = base_delay

    @staticmethod
    def _build_messages(system_prompt: str, user_message: str) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

    @staticmethod
    def _is_openrouter_base_url(base_url: str | None) -> bool:
        normalized_base = (base_url or "").strip().lower()
        return "openrouter.ai" in normalized_base

    @staticmethod
    def _is_dashscope_base_url(base_url: str | None) -> bool:
        normalized_base = (base_url or "").strip().lower()
        return "dashscope.aliyuncs.com" in normalized_base

    @classmethod
    def _provider_name_from_base_url(cls, base_url: str | None) -> str:
        normalized_base = (base_url or "").strip().lower()
        if not normalized_base:
            return "unknown"
        if cls._is_openrouter_base_url(normalized_base):
            return "openrouter"
        if cls._is_dashscope_base_url(normalized_base):
            return "dashscope"
        if "deepseek.com" in normalized_base:
            return "deepseek"
        if "opencode.ai" in normalized_base:
            return "opencode"
        return "unknown"

    @staticmethod
    def _extract_auth_error_detail(error: openai.AuthenticationError) -> str | None:
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            error_payload = body.get("error")
            if isinstance(error_payload, dict):
                message = error_payload.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()
            message = body.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return str(error).strip() or None

    @classmethod
    def _normalize_terminal_error(cls, error: Exception, route: _ModelRoute) -> Exception:
        if isinstance(error, openai.AuthenticationError):
            return LLMAuthenticationError(
                model=route.model,
                provider=cls._provider_name_from_base_url(route.base_url),
                original_error=error,
                detail=cls._extract_auth_error_detail(error),
            )
        return error

    @classmethod
    def _provider_extra_body(cls, route: _ModelRoute) -> dict[str, Any]:
        extra_body: dict[str, Any] = {}

        if cls._is_openrouter_base_url(route.base_url):
            extra_body["reasoning"] = {"effort": "none"}

        if cls._is_dashscope_base_url(route.base_url):
            extra_body["enable_thinking"] = False

        if route.extra_body:
            extra_body.update(route.extra_body)

        return extra_body

    @classmethod
    def _chat_completion_request_kwargs(
        cls,
        route: _ModelRoute,
        messages: list[dict[str, Any]],
        *,
        stream: bool,
        json_mode: bool = False,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
        extra_body_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": route.model,
            "messages": messages,
            "stream": stream,
        }
        if stream:
            kwargs["stream_options"] = {"include_usage": True}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        extra_body = cls._provider_extra_body(route)
        if extra_body_override:
            extra_body.update(extra_body_override)
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs

    @staticmethod
    def _message_length_summary(messages: list[dict[str, Any]]) -> list[int]:
        result = []
        for message in messages[:5]:
            c = message.get("content")
            if isinstance(c, str):
                result.append(len(c))
            elif isinstance(c, list):
                result.append(sum(len(p.get("text", "")) for p in c if isinstance(p, dict) and p.get("type") == "text"))
            else:
                result.append(0)
        return result

    @classmethod
    def _message_count_and_chars(cls, messages: list[dict[str, Any]]) -> tuple[int, int]:
        return len(messages), sum(
            len(cls._coerce_text(message.get("content"))) for message in messages
        )

    @staticmethod
    def _coerce_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
                    continue
                if isinstance(item, dict):
                    dict_text = item.get("text") or item.get("content")
                    if isinstance(dict_text, str):
                        parts.append(dict_text)
            return "".join(parts)
        text = getattr(value, "text", None)
        if isinstance(text, str):
            return text
        return str(value)

    @classmethod
    def _extract_response_content(cls, response: Any) -> str:
        choices = getattr(response, "choices", None)
        if not choices:
            return ""
        first_choice = choices[0]
        message = getattr(first_choice, "message", None)
        return cls._coerce_text(getattr(message, "content", None))

    @classmethod
    def _extract_chunk_content(cls, chunk: Any) -> str:
        choices = getattr(chunk, "choices", None)
        if not choices:
            return ""
        first_choice = choices[0]
        delta = getattr(first_choice, "delta", None)
        return cls._coerce_text(getattr(delta, "content", None))

    @classmethod
    def _extract_usage_details(cls, response_or_usage: Any) -> dict[str, Any] | None:
        usage = getattr(response_or_usage, "usage", None)
        if usage is None:
            usage = response_or_usage
        if usage is None:
            return None

        def _value(*names: str) -> int | None:
            for name in names:
                raw = getattr(usage, name, None)
                if raw is None and isinstance(usage, dict):
                    raw = usage.get(name)
                if isinstance(raw, int):
                    return raw
            return None

        input_tokens = _value("prompt_tokens", "input_tokens", "request_tokens", "input")
        output_tokens = _value("completion_tokens", "output_tokens", "response_tokens", "output")
        total_tokens = _value("total_tokens", "total")
        cached_tokens = _value("cached_tokens", "cache_read_input_tokens", "cache_read_tokens")
        cache_write_tokens = _value("cache_creation_input_tokens", "cache_write_tokens")

        details: dict[str, Any] = {}
        if input_tokens is not None:
            details["input"] = input_tokens
        if output_tokens is not None:
            details["output"] = output_tokens
        if cached_tokens is not None:
            details["cache_read_input_tokens"] = cached_tokens
        if cache_write_tokens is not None:
            details["cache_write_input_tokens"] = cache_write_tokens
        if total_tokens is not None:
            if (
                cached_tokens is not None
                and input_tokens is not None
                and output_tokens is not None
                and total_tokens == input_tokens + output_tokens
            ):
                details["total"] = total_tokens + cached_tokens
            else:
                details["total"] = total_tokens

        return details or None

    @classmethod
    def _extract_cost_details(cls, response_or_usage: Any) -> dict[str, Any] | None:
        usage = getattr(response_or_usage, "usage", None)
        if usage is None:
            usage = response_or_usage
        if usage is None:
            return None

        for attr in ("cost", "total_cost", "cost_usd"):
            raw = getattr(usage, attr, None)
            if raw is None and isinstance(usage, dict):
                raw = usage.get(attr)
            if isinstance(raw, int | float):
                return {"total": float(raw)}
        return None

    @classmethod
    def _to_text_result(cls, response: Any, model: str) -> LLMTextResult:
        return LLMTextResult(
            text=cls._extract_response_content(response),
            model=model,
            usage_details=cls._extract_usage_details(response),
            cost_details=cls._extract_cost_details(response),
        )

    def _completion_sync_with_fallback(
        self, messages: list[dict[str, Any]], *, stream: bool, json_mode: bool = False
    ) -> tuple[Any, str]:
        last_error: Exception | None = None
        last_route: _ModelRoute | None = None
        message_count, total_chars = self._message_count_and_chars(messages)
        for route in self._routes:
            try:
                response = route.sync_client.chat.completions.create(
                    **self._chat_completion_request_kwargs(route, messages, stream=stream, json_mode=json_mode)
                )
                return response, route.model
            except Exception as e:
                last_error = e
                logger.debug(
                    "SDK sync call failed; model=%s; stream=%s; message_count=%s; total_chars=%s; message_lengths=%s; error=%s",
                    route.model,
                    stream,
                    message_count,
                    total_chars,
                    self._message_length_summary(messages),
                    e,
                )
                last_route = route
        if last_error is not None:
            if last_route is not None:
                raise self._normalize_terminal_error(last_error, last_route)
            raise last_error
        raise RuntimeError("No LLM route configured")

    async def _completion_async_with_fallback(
        self, messages: list[dict[str, Any]], *, stream: bool, json_mode: bool = False
    ) -> tuple[Any, str]:
        last_error: Exception | None = None
        last_route: _ModelRoute | None = None
        message_count, total_chars = self._message_count_and_chars(messages)
        for route in self._routes:
            try:
                response = await route.async_client.chat.completions.create(
                    **self._chat_completion_request_kwargs(route, messages, stream=stream, json_mode=json_mode)
                )
                return response, route.model
            except Exception as e:
                last_error = e
                logger.debug(
                    "SDK async call failed; model=%s; stream=%s; message_count=%s; total_chars=%s; message_lengths=%s; error=%s",
                    route.model,
                    stream,
                    message_count,
                    total_chars,
                    self._message_length_summary(messages),
                    e,
                )
                last_route = route
        if last_error is not None:
            if last_route is not None:
                raise self._normalize_terminal_error(last_error, last_route)
            raise last_error
        raise RuntimeError("No LLM route configured")

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        *,
        retry_observer: LLMRetryObserver | None = None,
    ) -> str:
        """Call LLM for text response."""
        return self._chat_with_retries(
            system_prompt=system_prompt,
            user_message=user_message,
            retry_observer=retry_observer,
        )

    def chat_with_usage(
        self,
        system_prompt: str,
        user_message: str,
        *,
        retry_observer: LLMRetryObserver | None = None,
        json_mode: bool = False,
    ) -> LLMTextResult:
        """Call LLM for text response and preserve provider usage details."""
        return self._chat_with_retries(
            system_prompt=system_prompt,
            user_message=user_message,
            retry_observer=retry_observer,
            include_usage=True,
            json_mode=json_mode,
        )

    def chat_stream(
        self,
        system_prompt: str,
        user_message: str,
        *,
        retry_observer: LLMRetryObserver | None = None,
    ):
        """Call LLM for streaming text response."""
        yield from self._chat_stream_with_retries(
            system_prompt=system_prompt,
            user_message=user_message,
            retry_observer=retry_observer,
        )

    def pop_last_stream_usage_details(self) -> dict[str, Any] | None:
        usage_details = getattr(self, "_last_stream_usage_details", None)
        self._last_stream_usage_details = None
        return usage_details

    def pop_last_stream_cost_details(self) -> dict[str, Any] | None:
        cost_details = getattr(self, "_last_stream_cost_details", None)
        self._last_stream_cost_details = None
        return cost_details

    async def chat_stream_async(
        self,
        system_prompt: str,
        user_message: str,
        *,
        retry_observer: LLMRetryObserver | None = None,
    ):
        """Async streaming chat with the OpenAI-compatible SDK."""
        async for chunk in self._chat_stream_async_with_retries(
            system_prompt=system_prompt,
            user_message=user_message,
            retry_observer=retry_observer,
        ):
            yield chunk

    async def chat_stream_messages_async(
        self,
        messages: list[dict[str, Any]],
        *,
        retry_observer: LLMRetryObserver | None = None,
    ):
        """Async streaming chat with fully specified messages[] payload."""
        async for chunk in self._chat_stream_async_messages_with_retries(
            messages=messages,
            retry_observer=retry_observer,
        ):
            yield chunk

    async def stream_chat_with_tools_async(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        max_tokens: int | None = None,
        extra_body_override: dict[str, Any] | None = None,
    ):
        """Stream one assistant turn while surfacing OpenAI-style tool calls.

        Yields ``{"type": "text", "text": ...}`` for content deltas and one
        ``{"type": "tool_calls", "calls": [...]}`` event after the upstream
        stream finishes. Tool-call arguments arrive as string fragments keyed by
        ``index``; they are accumulated here so callers never re-assemble them.
        """
        last_error: Exception | None = None
        last_route: _ModelRoute | None = None

        for route in self._routes:
            yielded_any = False
            accumulator: dict[int, dict[str, Any]] = {}
            try:
                kwargs = self._chat_completion_request_kwargs(
                    route,
                    messages,
                    stream=True,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    extra_body_override=extra_body_override,
                )
                response = await route.async_client.chat.completions.create(**kwargs)
                async for chunk in response:
                    usage_details = self._extract_usage_details(chunk)
                    if usage_details:
                        self._last_stream_usage_details = usage_details
                        self._last_stream_cost_details = self._extract_cost_details(chunk)
                        continue
                    choices = getattr(chunk, "choices", None) or []
                    delta = getattr(choices[0], "delta", None) if choices else None
                    reasoning_text = None
                    if delta is not None:
                        reasoning_text = getattr(delta, "reasoning_content", None) or getattr(
                            delta, "reasoning", None
                        )
                    if isinstance(reasoning_text, str) and reasoning_text:
                        yielded_any = True
                        yield {"type": "reasoning", "text": reasoning_text}
                    content = self._extract_chunk_content(chunk)
                    if content:
                        yielded_any = True
                        yield {"type": "text", "text": content}
                    if not choices:
                        continue
                    tool_deltas = getattr(delta, "tool_calls", None) if delta else None
                    for tool_delta in tool_deltas or []:
                        index = getattr(tool_delta, "index", 0) or 0
                        slot = accumulator.setdefault(
                            index, {"id": "", "name": "", "arguments": ""}
                        )
                        call_id = getattr(tool_delta, "id", None)
                        if call_id:
                            slot["id"] = call_id
                        function = getattr(tool_delta, "function", None)
                        if function is None:
                            continue
                        name = getattr(function, "name", None)
                        if name:
                            slot["name"] = name
                        arguments = getattr(function, "arguments", None)
                        if arguments:
                            slot["arguments"] += arguments
                if accumulator:
                    yield {
                        "type": "tool_calls",
                        "calls": [accumulator[index] for index in sorted(accumulator)],
                    }
                return
            except Exception as exc:
                if yielded_any:
                    raise
                last_error = exc
                last_route = route
        if last_error is not None:
            if last_route is not None:
                raise self._normalize_terminal_error(last_error, last_route)
            raise last_error
        raise RuntimeError("No LLM route configured")

    async def _chat_stream_async_messages_with_retries(        self,
        messages: list[dict[str, Any]],
        *,
        retry_observer: LLMRetryObserver | None = None,
    ):
        """Async streaming with messages[] + retry logic using RetryExecutor."""
        executor = RetryExecutor(
            max_retries=self._max_retries,
            base_delay=self._base_delay,
            model=self._model_label,
            operation="async_stream_messages",
            retryable_exceptions=_RETRYABLE_OPENAI_EXCEPTIONS,
            retry_observer=retry_observer,
        )

        async def do_stream():
            last_error: Exception | None = None
            last_route: _ModelRoute | None = None
            message_count, total_chars = self._message_count_and_chars(messages)
            for route in self._routes:
                yielded_any = False
                try:
                    response = await route.async_client.chat.completions.create(
                        **self._chat_completion_request_kwargs(route, messages, stream=True)
                    )
                    async for chunk in response:
                        usage_details = self._extract_usage_details(chunk)
                        if usage_details:
                            self._last_stream_usage_details = usage_details
                            self._last_stream_cost_details = self._extract_cost_details(chunk)
                            continue
                        content = self._extract_chunk_content(chunk)
                        if content:
                            yielded_any = True
                            yield content
                    return
                except Exception as e:
                    if yielded_any:
                        raise
                    last_error = e
                    logger.debug(
                        "SDK async stream init failed; model=%s; message_count=%s; total_chars=%s; message_lengths=%s; error=%s",
                        route.model,
                        message_count,
                        total_chars,
                        self._message_length_summary(messages),
                        e,
                    )
                    last_route = route
            if last_error is not None:
                if last_route is not None:
                    raise self._normalize_terminal_error(last_error, last_route)
                raise last_error
            raise RuntimeError("No LLM route configured")

        async for item in executor.stream_async(do_stream):
            yield item

    async def _chat_stream_async_with_retries(
        self,
        system_prompt: str,
        user_message: str,
        *,
        retry_observer: LLMRetryObserver | None = None,
    ):
        """Async streaming with retry logic using RetryExecutor."""
        messages = self._build_messages(system_prompt, user_message)
        async for item in self._chat_stream_async_messages_with_retries(
            messages=messages,
            retry_observer=retry_observer,
        ):
            yield item

    def chat_json(
        self,
        system_prompt: str,
        user_message: str,
        default: dict[str, Any] = None,
        *,
        retry_observer: LLMRetryObserver | None = None,
    ) -> dict[str, Any]:
        """Call LLM and parse JSON response with safe fallback."""
        if default is None:
            default = {}

        try:
            response_result = self.chat_with_usage(
                system_prompt,
                user_message,
                retry_observer=retry_observer,
                json_mode=True,
            )
            response_text = response_result.text
            parsed_data = self._safe_parse_json(response_text, default)
            return {
                "parsed_data": parsed_data,
                "raw_response": response_text,
                "model": response_result.model,
                "success": True,
                "usage_details": response_result.usage_details,
                "cost_details": response_result.cost_details,
            }
        except (LLMCallError, LLMAuthenticationError):
            raise
        except Exception as e:
            logger.error("Unexpected error in chat_json: %s", e)
            return {
                "parsed_data": default,
                "raw_response": "",
                "model": self._model_label,
                "success": False,
                "error": str(e),
            }

    @staticmethod
    def _strip_response_wrappers(response: str) -> str:
        text = (response or "").strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]

        if text.endswith("```"):
            text = text[:-3]

        text = text.strip()
        if text.startswith("<think>"):
            closing_tag = text.find("</think>")
            if closing_tag >= 0:
                text = text[closing_tag + len("</think>") :].strip()
        return text

    @staticmethod
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

    def _safe_parse_json(self, response: str, default: dict[str, Any]) -> dict[str, Any]:
        """Parse JSON response with fallback to default."""
        if not response:
            logger.error("Empty response from LLM")
            return default

        text = self._strip_response_wrappers(response)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            candidate = self._extract_json_substring(text)
            if candidate:
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass
            logger.error("Invalid JSON response: %s. Response: %s", e, response[:200])
            return default

    def _chat_with_retries(
        self,
        system_prompt: str,
        user_message: str,
        *,
        retry_observer: LLMRetryObserver | None = None,
        include_usage: bool = False,
        json_mode: bool = False,
    ) -> str | LLMTextResult:
        """Call the chat provider with retries using RetryExecutor."""
        executor = RetryExecutor(
            max_retries=self._max_retries,
            base_delay=self._base_delay,
            model=self._model_label,
            operation="chat",
            retryable_exceptions=_RETRYABLE_OPENAI_EXCEPTIONS,
            retry_observer=retry_observer,
        )
        messages = self._build_messages(system_prompt, user_message)

        def do_chat() -> str | LLMTextResult:
            response, model = self._completion_sync_with_fallback(messages=messages, stream=False, json_mode=json_mode)
            result = self._to_text_result(response, model)
            return result if include_usage else result.text

        return executor.execute(do_chat)

    def _chat_stream_with_retries(
        self,
        system_prompt: str,
        user_message: str,
        *,
        retry_observer: LLMRetryObserver | None = None,
    ):
        """Call the streaming chat API with retries using RetryExecutor."""
        executor = RetryExecutor(
            max_retries=self._max_retries,
            base_delay=self._base_delay,
            model=self._model_label,
            operation="stream",
            retryable_exceptions=_RETRYABLE_OPENAI_EXCEPTIONS,
            retry_observer=retry_observer,
        )
        messages = self._build_messages(system_prompt, user_message)

        def do_stream():
            last_error: Exception | None = None
            last_route: _ModelRoute | None = None
            message_count, total_chars = self._message_count_and_chars(messages)
            for route in self._routes:
                yielded_any = False
                try:
                    response = route.sync_client.chat.completions.create(
                        **self._chat_completion_request_kwargs(route, messages, stream=True)
                    )
                    for chunk in response:
                        usage_details = self._extract_usage_details(chunk)
                        if usage_details:
                            self._last_stream_usage_details = usage_details
                            self._last_stream_cost_details = self._extract_cost_details(chunk)
                            continue
                        content = self._extract_chunk_content(chunk)
                        if content:
                            yielded_any = True
                            yield content
                    return
                except Exception as e:
                    if yielded_any:
                        raise
                    last_error = e
                    logger.debug(
                        "SDK sync stream init failed; model=%s; message_count=%s; total_chars=%s; message_lengths=%s; error=%s",
                        route.model,
                        message_count,
                        total_chars,
                        self._message_length_summary(messages),
                        e,
                    )
                    last_route = route
            if last_error is not None:
                if last_route is not None:
                    raise self._normalize_terminal_error(last_error, last_route)
                raise last_error
            raise RuntimeError("No LLM route configured")

        yield from executor.stream(do_stream)
