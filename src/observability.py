"""Unified Langfuse runtime and LLM observability helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, cast

from src.core.config import settings
from src.core.logging_context import (
    ObservabilityContextSnapshot,
    bind_log_context,
    capture_observability_context,
    reset_llm_call_id,
    set_llm_call_id,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class _NoopLangfuseClient:
    """Fallback client that safely ignores tracing calls."""

    def update_current_trace(self, *args: Any, **kwargs: Any) -> None:
        return None

    def update_current_observation(self, *args: Any, **kwargs: Any) -> None:
        return None

    def update_current_generation(self, *args: Any, **kwargs: Any) -> None:
        return None

    def flush(self) -> None:
        return None


_NOOP_CLIENT = _NoopLangfuseClient()

_TRACE_USER_ID_ATTR = "user.id"
_TRACE_SESSION_ID_ATTR = "session.id"
_TRACE_TAGS_ATTR = "langfuse.trace.tags"
_TRACE_METADATA_PREFIX = "langfuse.trace.metadata"

try:  # pragma: no cover - import availability depends on environment
    import langfuse as _langfuse_module
    from langfuse import get_client as _langfuse_get_client
    from langfuse import observe as _langfuse_observe
    from langfuse import propagate_attributes as _langfuse_propagate_attributes
except Exception:  # pragma: no cover - environment-dependent
    _langfuse_module = None
    _langfuse_get_client = None
    _langfuse_observe = None
    _langfuse_propagate_attributes = None


@dataclass(frozen=True)
class LLMCallRequest:
    operation: str
    route_role: str
    provider: str
    model: str
    sdk_kind: str
    input_payload: Any | None = None
    model_parameters: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None
    session_id: str | None = None
    chat_id: str | None = None
    turn_id: str | None = None
    candidate_id: str | None = None
    character_id: str | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    # Redaction boundary for evaluation calls: when disabled the observation
    # input/output must stay null (never the raw prompt or completion).
    capture_input: bool = True
    capture_output: bool = True


@dataclass
class LLMGenerationHandle:
    request: LLMCallRequest
    llm_call_id: str
    observation: Any | None = None
    started_at: float = field(default_factory=time.monotonic)
    chunk_count: int = 0
    output_chars: int = 0
    first_chunk_ms: int | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class VoiceCallRequest:
    operation: str
    provider: str
    model: str
    input_payload: Any | None = None
    model_parameters: dict[str, Any] = field(default_factory=dict)
    user_id: str | None = None
    session_id: str | None = None
    chat_id: str | None = None
    turn_id: str | None = None
    candidate_id: str | None = None
    character_id: str | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VoiceGenerationHandle:
    request: VoiceCallRequest
    voice_call_id: str
    observation: Any | None = None
    started_at: float = field(default_factory=time.monotonic)
    audio_delta_count: int = 0
    output_bytes: int = 0
    input_characters: int = 0


@dataclass(frozen=True)
class LLMRetryEvent:
    attempt: int
    max_retries: int
    delay_ms: int
    retryable: bool
    error_type: str
    error_message: str


class LLMRetryObserver(Protocol):
    def __call__(self, event: LLMRetryEvent) -> None: ...


class LLMFirstChunkTimeoutError(TimeoutError):
    """Raised when a generation stream produces no first chunk in time."""

    def __init__(self, *, operation: str, model: str, timeout_s: float) -> None:
        self.operation = operation
        self.model = model
        self.timeout_s = timeout_s
        super().__init__(
            f"{operation} first chunk timeout after {timeout_s:.1f}s for model {model}"
        )


_OPERATION_LABELS = {
    "chat.reply.stream": "Chat reply stream",
    "chat.reply.regen": "Chat reply regeneration",
    "chat.reply.edit": "Chat reply edit",
    "chat.title.generate": "Chat title generation",
    "learning.translate.mixed_to_english": "Mixed-input translation",
    "learning.feedback_card.generate": "Feedback card generation",
    "learning.reply_card.generate": "Reply card generation",
    "learning.reply_suggestions.generate": "Reply suggestion generation",
    "learning.word_card.generate": "Word card generation",
    "memory.group_judge.generate": "Memory group judgment",
    "memory.episodic_manage.generate": "Episodic memory management",
    "memory.summary.generate": "Semantic memory extraction",
    "memory.search": "Memory search",
    "memory.narrative_assign": "Narrative memory assignment",
    "memory.consolidate.batch": "Memory consolidation",
    "memory.embed": "Memory embedding",
    "voice.tts.stream": "Voice TTS stream",
    "voice.tts.stream_segments": "Voice segmented TTS stream",
    "voice.stt.transcribe": "Voice STT transcription",
    "voice.clone.create": "Voice clone creation",
    "voice.clone.delete": "Voice clone deletion",
}


def is_langfuse_available() -> bool:
    """Return True when Langfuse is enabled and importable."""

    return bool(
        settings.LANGFUSE_ENABLED
        and _langfuse_module is not None
        and _langfuse_get_client is not None
        and _langfuse_observe is not None
    )


def _ensure_langfuse_env() -> None:
    if settings.LANGFUSE_PUBLIC_KEY:
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.LANGFUSE_PUBLIC_KEY
    if settings.LANGFUSE_SECRET_KEY:
        os.environ["LANGFUSE_SECRET_KEY"] = settings.LANGFUSE_SECRET_KEY
    if settings.LANGFUSE_BASE_URL:
        os.environ["LANGFUSE_BASE_URL"] = settings.LANGFUSE_BASE_URL
    elif settings.LANGFUSE_HOST:
        os.environ["LANGFUSE_HOST"] = settings.LANGFUSE_HOST


def init_langfuse() -> bool:
    """Initialize Langfuse when available."""

    if not settings.LANGFUSE_ENABLED:
        logger.info("Langfuse tracing disabled by configuration.")
        return False

    if not is_langfuse_available():
        logger.warning("Langfuse enabled but package is not installed.")
        return False

    try:
        _ensure_langfuse_env()
        if hasattr(_langfuse_module, "init"):
            _langfuse_module.init()
        elif _langfuse_get_client is not None:
            _ = _langfuse_get_client()
        logger.info("Langfuse initialized successfully", extra={"event": "langfuse.init"})
        return True
    except Exception as exc:  # pragma: no cover - runtime/network dependent
        logger.warning(
            "Langfuse initialization failed; tracing will degrade to no-op: %s",
            exc,
            extra={"event": "langfuse.init_failed"},
        )
        return False


def observe(*decorator_args: Any, **decorator_kwargs: Any):
    """Decorator compatible with Langfuse's observe API."""

    if settings.LANGFUSE_ENABLED and _langfuse_observe is not None:
        return _langfuse_observe(*decorator_args, **decorator_kwargs)

    def _identity(func: F) -> F:
        return func

    if len(decorator_args) == 1 and callable(decorator_args[0]) and not decorator_kwargs:
        return cast(F, decorator_args[0])

    return _identity


@contextmanager
def _noop_context_manager() -> Iterator[None]:
    yield


def propagate_attributes(*args: Any, **kwargs: Any):
    """Langfuse propagate_attributes wrapper with a safe fallback."""

    if settings.LANGFUSE_ENABLED and _langfuse_propagate_attributes is not None:
        return _langfuse_propagate_attributes(*args, **kwargs)
    return _noop_context_manager()


def get_client() -> Any:
    """Return Langfuse client when available, otherwise a no-op client."""

    if not is_langfuse_available() or _langfuse_get_client is None:
        return _NOOP_CLIENT

    try:
        _ensure_langfuse_env()
        client = _langfuse_get_client()
    except Exception:  # pragma: no cover - runtime dependent
        return _NOOP_CLIENT
    return client if client is not None else _NOOP_CLIENT


def build_model_label(*, provider: str, model: str) -> str:
    return f"{provider}/{model}"


def build_chat_session_id(chat_id: str) -> str:
    return f"chat:{chat_id}"


def build_memory_batch_session_id(user_id: str) -> str:
    return f"memory:{user_id}:batch"


def current_trace_context(parent_observation: Any | None = None) -> dict[str, str] | None:
    parent_span_id = getattr(parent_observation, "id", None)
    trace_id = getattr(parent_observation, "trace_id", None)

    if (
        not isinstance(trace_id, str)
        or not trace_id
        or not isinstance(parent_span_id, str)
        or not parent_span_id
    ):
        client = get_client()
        if not isinstance(trace_id, str) or not trace_id:
            get_trace_id = getattr(client, "get_current_trace_id", None)
            trace_id = get_trace_id() if callable(get_trace_id) else None
        if not isinstance(parent_span_id, str) or not parent_span_id:
            get_observation_id = getattr(client, "get_current_observation_id", None)
            parent_span_id = get_observation_id() if callable(get_observation_id) else None

    if isinstance(trace_id, str) and trace_id and isinstance(parent_span_id, str) and parent_span_id:
        return {
            "trace_id": trace_id,
            "parent_span_id": parent_span_id,
        }
    return None


def _normalize_propagated_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    if not metadata:
        return {}

    normalized: dict[str, str] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        normalized[str(key)] = str(value)
    return normalized


def _normalize_trace_tags(tags: list[str] | tuple[str, ...] | None) -> list[str]:
    if not tags:
        return []
    normalized: list[str] = []
    for value in tags:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped or len(stripped) > 200:
            continue
        normalized.append(stripped)
    return normalized


def _apply_trace_attributes_to_observation(
    observation: Any,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Attach Langfuse trace-level attributes to manual observations.

    Langfuse's public propagation helper writes trace attributes onto the current span.
    Manual observations created via ``start_observation`` do not become current spans,
    so we mirror the same attributes directly on the wrapped OTEL span.
    """

    otel_span = getattr(observation, "_otel_span", None)
    if otel_span is None or not hasattr(otel_span, "set_attribute"):
        return

    if isinstance(user_id, str):
        stripped_user_id = user_id.strip()
        if stripped_user_id and len(stripped_user_id) <= 200:
            otel_span.set_attribute(_TRACE_USER_ID_ATTR, stripped_user_id)

    if isinstance(session_id, str):
        stripped_session_id = session_id.strip()
        if stripped_session_id and len(stripped_session_id) <= 200:
            otel_span.set_attribute(_TRACE_SESSION_ID_ATTR, stripped_session_id)

    normalized_tags = _normalize_trace_tags(tags)
    if normalized_tags:
        otel_span.set_attribute(_TRACE_TAGS_ATTR, normalized_tags)

    for key, value in _normalize_propagated_metadata(metadata).items():
        if len(key) > 200:
            continue
        otel_span.set_attribute(f"{_TRACE_METADATA_PREFIX}.{key}", value)


@contextmanager
def observability_scope(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Propagate Langfuse attributes across nested work."""

    with propagate_attributes(
        user_id=user_id,
        session_id=session_id,
        tags=list(tags or []),
        metadata=_normalize_propagated_metadata(metadata),
    ):
        yield


@contextmanager
def start_workflow_observation(
    *,
    name: str,
    input_payload: Any | None = None,
    trace_context: dict[str, str] | None = None,
    manual: bool = False,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    metadata: dict[str, Any] | None = None,
):
    """Create a current root observation and propagate trace attributes inside it."""

    client = get_client()
    starter = getattr(client, "start_as_current_observation", None)
    manual_starter = getattr(client, "start_observation", None)
    raw_metadata = metadata or {}

    if manual:
        observation = None
        if callable(manual_starter):
            with observability_scope(
                user_id=user_id,
                session_id=session_id,
                tags=tags,
                metadata=raw_metadata,
            ):
                observation = manual_starter(
                    name=name,
                    as_type="span",
                    trace_context=trace_context,
                    input=input_payload,
                    metadata=raw_metadata,
                )
                _apply_trace_attributes_to_observation(
                    observation,
                    user_id=user_id,
                    session_id=session_id,
                    tags=tags,
                    metadata=raw_metadata,
                )
        try:
            yield observation
        finally:
            if observation is not None and hasattr(observation, "end"):
                observation.end()
        return

    if not callable(starter):
        with observability_scope(
            user_id=user_id,
            session_id=session_id,
            tags=tags,
            metadata=raw_metadata,
        ):
            yield None
        return

    with starter(
        name=name,
        as_type="span",
        trace_context=trace_context,
        input=input_payload,
        metadata=raw_metadata,
        end_on_exit=True,
    ) as observation:
        with observability_scope(
            user_id=user_id,
            session_id=session_id,
            tags=tags,
            metadata=raw_metadata,
        ):
            yield observation


def update_observation_safe(observation: Any, **kwargs: Any) -> None:
    if observation is not None and hasattr(observation, "update"):
        observation.update(**kwargs)


def create_task_in_observability_context(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str,
    snapshot: ObservabilityContextSnapshot | None = None,
) -> asyncio.Task[Any]:
    """Create an asyncio task with an explicit copied context."""

    active_snapshot = snapshot or capture_observability_context()
    return active_snapshot.run(lambda: asyncio.create_task(coro, name=name))


def capture_context_snapshot() -> ObservabilityContextSnapshot:
    """Alias used by async/task orchestration helpers."""

    return capture_observability_context()


def _update_current_generation(**kwargs: Any) -> None:
    client = get_client()
    updater = getattr(client, "update_current_generation", None)
    if callable(updater):
        updater(**kwargs)
        return
    fallback = getattr(client, "update_current_observation", None)
    if callable(fallback):
        fallback(**kwargs)


def _request_metadata(request: LLMCallRequest, llm_call_id: str) -> dict[str, Any]:
    metadata = {
        "llm_call_id": llm_call_id,
        "operation": request.operation,
        "route_role": request.route_role,
        "model": build_model_label(provider=request.provider, model=request.model),
        "sdk_kind": request.sdk_kind,
    }
    if request.chat_id:
        metadata["chat_id"] = request.chat_id
    if request.turn_id:
        metadata["turn_id"] = request.turn_id
    if request.candidate_id:
        metadata["candidate_id"] = request.candidate_id
    if request.character_id:
        metadata["character_id"] = request.character_id
    metadata.update(request.metadata)
    return metadata


def _voice_request_metadata(request: VoiceCallRequest, voice_call_id: str) -> dict[str, Any]:
    metadata = {
        "voice_call_id": voice_call_id,
        "operation": request.operation,
        "provider": request.provider,
        "model": build_model_label(provider=request.provider, model=request.model),
    }
    if request.chat_id:
        metadata["chat_id"] = request.chat_id
    if request.turn_id:
        metadata["turn_id"] = request.turn_id
    if request.candidate_id:
        metadata["candidate_id"] = request.candidate_id
    if request.character_id:
        metadata["character_id"] = request.character_id
    metadata.update(request.metadata)
    return metadata


def start_voice_generation(
    request: VoiceCallRequest,
    *,
    trace_context: dict[str, str] | None = None,
) -> VoiceGenerationHandle:
    """Create a manual provider-neutral voice generation observation."""

    voice_call_id = str(uuid.uuid4())
    metadata = _voice_request_metadata(request, voice_call_id)
    observation = None
    starter = getattr(get_client(), "start_observation", None)
    if callable(starter):
        observation = starter(
            trace_context=trace_context,
            name=request.operation,
            as_type="generation",
            input=request.input_payload,
            model=build_model_label(provider=request.provider, model=request.model),
            model_parameters=request.model_parameters or None,
            metadata=metadata,
        )
        _apply_trace_attributes_to_observation(
            observation,
            user_id=request.user_id,
            session_id=request.session_id,
            tags=request.tags,
            metadata=request.metadata,
        )
    return VoiceGenerationHandle(
        request=request,
        voice_call_id=voice_call_id,
        observation=observation,
    )


def mark_voice_audio_delta(handle: VoiceGenerationHandle, *, output_bytes: int) -> None:
    handle.audio_delta_count += 1
    handle.output_bytes += max(0, output_bytes)


def finish_voice_generation(
    handle: VoiceGenerationHandle,
    *,
    output: Any,
    usage_details: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload_metadata = {
        "voice_call_id": handle.voice_call_id,
        "audio_delta_count": handle.audio_delta_count,
        "output_bytes": handle.output_bytes,
        "elapsed_ms": int((time.monotonic() - handle.started_at) * 1000),
    }
    if metadata:
        payload_metadata.update(metadata)
    update_payload: dict[str, Any] = {
        "output": output,
        "metadata": payload_metadata,
    }
    if usage_details:
        update_payload["usage_details"] = usage_details
    update_observation_safe(handle.observation, **update_payload)
    if handle.observation is not None and hasattr(handle.observation, "end"):
        handle.observation.end()


def record_voice_generation_failure(
    handle: VoiceGenerationHandle,
    exc: Exception,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    payload_metadata = {
        "voice_call_id": handle.voice_call_id,
        "audio_delta_count": handle.audio_delta_count,
        "output_bytes": handle.output_bytes,
        "elapsed_ms": int((time.monotonic() - handle.started_at) * 1000),
        "error_type": type(exc).__name__,
    }
    if metadata:
        payload_metadata.update(metadata)
    update_observation_safe(
        handle.observation,
        level="ERROR",
        status_message=f"{operation_label(handle.request.operation)} failed: {type(exc).__name__}",
        metadata=payload_metadata,
    )
    if handle.observation is not None and hasattr(handle.observation, "end"):
        handle.observation.end()


def start_llm_generation(
    request: LLMCallRequest,
    *,
    manual: bool = False,
    trace_context: dict[str, str] | None = None,
) -> LLMGenerationHandle:
    """Create the in-flight tracking handle for the current generation."""

    llm_call_id = str(uuid.uuid4())
    metadata = _request_metadata(request, llm_call_id)
    observation_input = request.input_payload if request.capture_input else None
    observation = None
    if manual:
        starter = getattr(get_client(), "start_observation", None)
        if callable(starter):
            observation = starter(
                trace_context=trace_context,
                name=request.operation,
                as_type="generation",
                input=observation_input,
                model=build_model_label(provider=request.provider, model=request.model),
                model_parameters=request.model_parameters or None,
                metadata=metadata,
            )
            _apply_trace_attributes_to_observation(
                observation,
                user_id=request.user_id,
                session_id=request.session_id,
                tags=request.tags,
                metadata=request.metadata,
            )
    handle = LLMGenerationHandle(
        request=request,
        llm_call_id=llm_call_id,
        observation=observation,
    )
    if observation is None:
        _update_current_generation(
            name=request.operation,
            input=observation_input,
            model=build_model_label(provider=request.provider, model=request.model),
            model_parameters=request.model_parameters or None,
            metadata=metadata,
        )
    return handle


def _update_handle_generation(handle: LLMGenerationHandle, **kwargs: Any) -> None:
    if handle.observation is not None and hasattr(handle.observation, "update"):
        handle.observation.update(**kwargs)
        return
    _update_current_generation(**kwargs)


def mark_llm_stream_chunk(handle: LLMGenerationHandle, chunk: str) -> None:
    handle.chunk_count += 1
    handle.output_chars += len(chunk or "")
    if handle.first_chunk_ms is None:
        handle.first_chunk_ms = int((time.monotonic() - handle.started_at) * 1000)
        _update_handle_generation(
            handle,
            metadata={
                "llm_call_id": handle.llm_call_id,
                "first_chunk_ms": handle.first_chunk_ms,
                "chunk_count": handle.chunk_count,
            }
        )


def record_llm_retry(handle: LLMGenerationHandle, event: LLMRetryEvent) -> None:
    """Update generation metadata and emit a sparse retry log."""

    handle.retry_count = max(handle.retry_count, event.attempt)
    _update_handle_generation(
        handle,
        metadata={
            "llm_call_id": handle.llm_call_id,
            "retry_count": handle.retry_count,
            "retry_delay_ms": event.delay_ms,
            "retry_error_type": event.error_type,
        }
    )
    if not settings.LLM_LOG_ENABLED:
        return

    logger.warning(
        "%s attempt %s failed. Retrying in %s ms. Reason: %s.",
        operation_label(handle.request.operation),
        event.attempt,
        event.delay_ms,
        event.error_message or event.error_type,
        extra={
            "log_module": "llm",
            "event": "llm.request.retry",
            "llm_call_id": handle.llm_call_id,
            "operation": handle.request.operation,
            "route_role": handle.request.route_role,
            "model": build_model_label(
                provider=handle.request.provider,
                model=handle.request.model,
            ),
            "sdk_kind": handle.request.sdk_kind,
            "user_id": handle.request.user_id,
            "chat_id": handle.request.chat_id,
            "turn_id": handle.request.turn_id,
            "candidate_id": handle.request.candidate_id,
            "character_id": handle.request.character_id,
        },
    )


def finish_llm_generation(
    handle: LLMGenerationHandle,
    *,
    output: Any,
    usage_details: dict[str, Any] | None = None,
    cost_details: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "output": output if handle.request.capture_output else None,
        "metadata": {
            "llm_call_id": handle.llm_call_id,
            "chunk_count": handle.chunk_count,
            "output_chars": handle.output_chars,
            "retry_count": handle.retry_count,
        },
    }
    if handle.first_chunk_ms is not None:
        payload["metadata"]["first_chunk_ms"] = handle.first_chunk_ms
    if usage_details:
        payload["usage_details"] = usage_details
    if cost_details:
        payload["cost_details"] = cost_details
    _update_handle_generation(handle, **payload)
    if handle.observation is not None and hasattr(handle.observation, "end"):
        handle.observation.end()


def record_empty_generation(handle: LLMGenerationHandle) -> None:
    _update_handle_generation(
        handle,
        level="WARNING",
        status_message=f"{operation_label(handle.request.operation)} completed without chunks.",
        output="",
        metadata={"llm_call_id": handle.llm_call_id},
    )
    if handle.observation is not None and hasattr(handle.observation, "end"):
        handle.observation.end()
    if not settings.LLM_LOG_ENABLED:
        return

    logger.warning(
        "%s completed without chunks.",
        operation_label(handle.request.operation),
        extra=_log_extra(handle, event="llm.request.empty_response"),
    )


def record_cancelled_generation(handle: LLMGenerationHandle) -> None:
    _update_handle_generation(
        handle,
        level="WARNING",
        status_message=f"{operation_label(handle.request.operation)} was cancelled.",
        metadata={"llm_call_id": handle.llm_call_id},
    )
    if handle.observation is not None and hasattr(handle.observation, "end"):
        handle.observation.end()
    if not settings.LLM_LOG_ENABLED:
        return

    logger.warning(
        "%s was cancelled before completion.",
        operation_label(handle.request.operation),
        extra=_log_extra(handle, event="llm.request.cancelled"),
    )


def record_timeout_generation(handle: LLMGenerationHandle, timeout_s: float) -> None:
    message = (
        f"{operation_label(handle.request.operation)} timed out: "
        f"no first chunk was received within {timeout_s:.1f} seconds."
    )
    _update_handle_generation(
        handle,
        level="ERROR",
        status_message=message,
        metadata={"llm_call_id": handle.llm_call_id, "timeout_s": timeout_s},
    )
    if handle.observation is not None and hasattr(handle.observation, "end"):
        handle.observation.end()
    if not settings.LLM_LOG_ENABLED:
        return

    logger.error(
        message,
        extra=_log_extra(
            handle,
            event="llm.request.timeout",
        ),
    )


def record_llm_failure(handle: LLMGenerationHandle, exc: Exception) -> None:
    """Attach the terminal failure to Langfuse and emit a sparse summary log."""

    message = summarize_llm_exception(handle.request.operation, exc)
    upstream_status = extract_upstream_status(exc)
    upstream_code = extract_upstream_code(exc)
    _update_handle_generation(
        handle,
        level="ERROR",
        status_message=message,
        metadata={
            "llm_call_id": handle.llm_call_id,
            "retry_count": handle.retry_count,
            "upstream_status": upstream_status,
            "upstream_code": upstream_code,
            "error_type": type(exc).__name__,
        },
    )
    if handle.observation is not None and hasattr(handle.observation, "end"):
        handle.observation.end()
    if not settings.LLM_LOG_ENABLED:
        return

    logger.error(
        message,
        extra=_log_extra(
            handle,
            event="llm.request.error",
            upstream_status=upstream_status,
            upstream_code=upstream_code,
        ),
    )


def extract_upstream_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code

    original = getattr(exc, "original_error", None)
    response = getattr(original, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code
    return None


def extract_upstream_code(exc: Exception) -> str | None:
    for candidate in (exc, getattr(exc, "original_error", None)):
        body = getattr(candidate, "body", None)
        if isinstance(body, dict):
            error_payload = body.get("error")
            if isinstance(error_payload, dict):
                error_type = error_payload.get("type")
                if isinstance(error_type, str) and error_type.strip():
                    return error_type.strip()
            error_code = body.get("code")
            if isinstance(error_code, str) and error_code.strip():
                return error_code.strip()
    return None


def summarize_llm_exception(operation: str, exc: Exception) -> str:
    label = operation_label(operation)
    if type(exc).__name__ == "LLMAuthenticationError" and hasattr(exc, "public_message"):
        return (
            f"{label} failed: the upstream model rejected authentication. "
            "Check the active API key and base URL."
        )
    if isinstance(exc, asyncio.TimeoutError):
        return f"{label} timed out before the upstream model returned a usable response."
    return f"{label} was interrupted by an unexpected error after the request was sent."


def operation_label(operation: str) -> str:
    return _OPERATION_LABELS.get(operation, operation.replace(".", " ").replace("_", " ").title())


def _log_extra(
    handle: LLMGenerationHandle,
    *,
    event: str,
    upstream_status: int | None = None,
    upstream_code: str | None = None,
) -> dict[str, Any]:
    return {
        "log_module": "llm",
        "event": event,
        "llm_call_id": handle.llm_call_id,
        "operation": handle.request.operation,
        "route_role": handle.request.route_role,
        "model": build_model_label(
            provider=handle.request.provider,
            model=handle.request.model,
        ),
        "sdk_kind": handle.request.sdk_kind,
        "user_id": handle.request.user_id,
        "chat_id": handle.request.chat_id,
        "turn_id": handle.request.turn_id,
        "candidate_id": handle.request.candidate_id,
        "character_id": handle.request.character_id,
        "elapsed_ms": int((time.monotonic() - handle.started_at) * 1000),
        "upstream_status": upstream_status,
        "upstream_code": upstream_code,
    }


@contextmanager
def bind_llm_generation_context(handle: LLMGenerationHandle) -> Iterator[None]:
    """Bind llm-specific log context for the current generation."""

    token = set_llm_call_id(handle.llm_call_id)
    with bind_log_context(
        operation=handle.request.operation,
        route_role=handle.request.route_role,
        user_id=handle.request.user_id,
        chat_id=handle.request.chat_id,
        turn_id=handle.request.turn_id,
        candidate_id=handle.request.candidate_id,
        character_id=handle.request.character_id,
    ):
        try:
            yield
        finally:
            reset_llm_call_id(token)


__all__ = [
    "LLMCallRequest",
    "LLMFirstChunkTimeoutError",
    "LLMGenerationHandle",
    "LLMRetryEvent",
    "LLMRetryObserver",
    "ObservabilityContextSnapshot",
    "bind_llm_generation_context",
    "build_chat_session_id",
    "build_memory_batch_session_id",
    "build_model_label",
    "capture_context_snapshot",
    "current_trace_context",
    "create_task_in_observability_context",
    "finish_llm_generation",
    "get_client",
    "init_langfuse",
    "is_langfuse_available",
    "mark_llm_stream_chunk",
    "observability_scope",
    "observe",
    "operation_label",
    "propagate_attributes",
    "record_cancelled_generation",
    "record_empty_generation",
    "record_llm_failure",
    "record_llm_retry",
    "record_timeout_generation",
    "start_workflow_observation",
    "start_llm_generation",
    "update_observation_safe",
]
