"""Shared chat generation seam.

Single source of truth for building LLM generation inputs (system prompt +
messages) across all production paths (chat stream, regen/edit/continue,
realtime voice) and the offline evaluation replay runner.

Rules (phase18 contract):
- ``_load_llm_history_messages_from_leaf`` and ``_build_user_message_with_images``
  stay in ``chat_service.py`` (module top level) and are injected into this seam
  by every caller, so this module never imports ``chat_service`` (no cycles).
- The preferred-expressions prompt builder is dependency-injected and defaults
  to the production ``ChatLearningService.build_preferred_expressions_prompt``.
- ``channel_policy="voice"`` only appends ``VOICE_CHANNEL_POLICY`` after the
  base chat system prompt and strips multimodal parts; it never changes the
  base chat prompt itself.
- This seam only builds inputs and (later) performs a single generation; it
  never persists, never triggers growth/memory/proactive side effects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from src.core.llm_registry import ResolvedLLMRoute
from src.memory_system import MemoryRecord
from src.memory_system.prompts import MEMORY_ANSWER_PROMPT
from src.schemas.chat import ChatImageRef
from src.services.chat_learning_service import ChatLearningService
from src.services.llm_gateway_service import LLMGatewayService, RetryPolicy

LIFE_PERSPECTIVE_INSTRUCTION = (
    "When it feels natural, you may let your unique life experiences "
    "and the perspectives they've shaped subtly inform your responses—"
    "there's no need to force it."
)

ChannelPolicy = Literal["chat", "voice"]

VOICE_CHANNEL_POLICY = """
Voice channel policy:
- Write only natural, conversational plain text that can be read aloud verbatim.
- Keep most turns to one or two short sentences unless the user explicitly asks for detail.
- Do not use Markdown, headings, bullet points, numbered lists, code blocks, brackets, or stage directions.
- Phrase dates, numbers, abbreviations, and symbols so they sound natural when spoken.
- Respond directly without narrating formatting or turn-taking instructions.
""".strip()

_MAX_EPISODIC_IN_PROMPT = 5
_MAX_SEMANTIC_IN_PROMPT = 5


@dataclass(frozen=True)
class ChatGenerationContext:
    """All inputs needed to build one chat generation request.

    ``memory_active`` is a runner/orchestration control (also what makes the
    Memory Utility Delta on/off pair share one context identity); it is not
    part of the case context fingerprint.
    """

    character_system_prompt: str
    history_messages: list[dict[str, Any]]
    user_message: str
    memories: dict[str, list[MemoryRecord]]
    use_all_semantic: bool
    preferred_expressions: list[str]
    preferred_expressions_prompt_builder: Callable[[list[str] | None], str | None] = field(
        default=ChatLearningService.build_preferred_expressions_prompt
    )
    chat_images: tuple[ChatImageRef, ...] = ()
    memory_active: bool = True
    channel_policy: ChannelPolicy = "chat"


def _validate_channel_policy(channel_policy: str) -> None:
    if channel_policy not in ("chat", "voice"):
        raise ValueError(f"invalid channel_policy: {channel_policy!r} (expected 'chat' or 'voice')")


def build_llm_system_prompt(context: ChatGenerationContext) -> str:
    """Build the system prompt exactly as the production chat path does.

    Byte-level parity with the historical ``chat_service._build_llm_system_prompt``
    is enforced by prompt parity tests; do not reorder or reword sections.
    """
    _validate_channel_policy(context.channel_policy)

    parts: list[str] = []

    parts.append((context.character_system_prompt or "").strip())
    parts.append("")
    parts.append(LIFE_PERSPECTIVE_INSTRUCTION)

    if context.memory_active:
        parts.append("")
        parts.append(MEMORY_ANSWER_PROMPT.strip())
        parts.append("")
        parts.append("Retrieved memories (may be empty):")

        parts.append("Episodic:")
        episodic = context.memories.get("episodic", [])
        if episodic:
            for i, mem in enumerate(episodic[:_MAX_EPISODIC_IN_PROMPT], 1):
                parts.append(f"{i}. {mem.text}")
        else:
            parts.append("(none)")

        parts.append("")
        parts.append("Semantic:")
        semantic = context.memories.get("semantic", [])
        semantic_for_prompt = semantic if context.use_all_semantic else semantic[:_MAX_SEMANTIC_IN_PROMPT]
        if semantic_for_prompt:
            for i, mem in enumerate(semantic_for_prompt, 1):
                parts.append(f"{i}. {mem.text}")
        else:
            parts.append("(none)")

    preferred_prompt = context.preferred_expressions_prompt_builder(context.preferred_expressions)
    if preferred_prompt:
        parts.append("")
        parts.append(preferred_prompt)

    prompt = "\n".join(parts).strip()
    if context.channel_policy == "voice":
        prompt = f"{prompt.rstrip()}\n\n{VOICE_CHANNEL_POLICY}"
    return prompt


def strip_image_parts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop image_url parts from multimodal messages (voice channel is text-only).

    Messages that lose all content parts are removed entirely.
    """
    sanitized: list[dict[str, Any]] = []

    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            sanitized.append(message)
            continue

        text_parts = [
            part
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        if not text_parts:
            continue

        sanitized.append({**message, "content": text_parts})

    return sanitized


def build_generation_messages(
    context: ChatGenerationContext,
    *,
    user_message_builder: Callable[[str, list[ChatImageRef] | None], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the full messages list for one generation call.

    ``user_message_builder`` must be the canonical production builder
    (``chat_service._build_user_message_with_images``); it is injected so this
    module never imports ``chat_service``.

    For ``channel_policy="voice"`` history image parts are stripped and no
    images are attached, matching the realtime production path.
    """
    _validate_channel_policy(context.channel_policy)

    system_prompt = build_llm_system_prompt(context)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]

    history_messages = context.history_messages
    chat_images: list[ChatImageRef] | None = list(context.chat_images)
    if context.channel_policy == "voice":
        history_messages = strip_image_parts(history_messages)
        chat_images = None

    messages.extend(history_messages)
    messages.append(user_message_builder(context.user_message, chat_images))
    return messages


@dataclass(frozen=True)
class ChatGenerationResult:
    """Outcome of one side-effect-free generation call."""

    text: str
    provider: str
    model: str
    model_type_label: str


class ChatGenerationService:
    """Single side-effect-free generation through the shared gateway.

    Used by the evaluation replay runner. Production paths keep their own
    orchestration (persistence, growth, background tasks) and never go through
    this adapter; replay is distinguished by ``side_effects=disabled``
    (this class simply performs no writes) and dedicated operation labels.
    """

    def __init__(self, gateway_service: LLMGatewayService | None = None) -> None:
        self._gateway = gateway_service or LLMGatewayService()

    async def generate_once(
        self,
        context: ChatGenerationContext,
        *,
        route: ResolvedLLMRoute,
        operation: str,
        user_message_builder: Callable[[str, list[ChatImageRef] | None], dict[str, Any]],
        retry_policy: RetryPolicy = "none",
        capture_input: bool = True,
        capture_output: bool = True,
        trace_context: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> ChatGenerationResult:
        """Run one generation for the context and collect the full output.

        The total-call timeout (including stream collection) is enforced here
        at the call boundary; provider-level retry is fixed by retry_policy
        and must not be re-added by callers.
        """
        messages = build_generation_messages(context, user_message_builder=user_message_builder)

        async def _collect() -> list[str]:
            chunks: list[str] = []
            async for chunk in self._gateway.stream_text(
                route=route,
                messages=messages,
                operation=operation,
                route_role="chat",
                trace_context=trace_context,
                retry_policy=retry_policy,
                capture_input=capture_input,
                capture_output=capture_output,
            ):
                chunks.append(chunk)
            return chunks

        if timeout_s is not None:
            chunks = await asyncio.wait_for(_collect(), timeout=timeout_s)
        else:
            chunks = await _collect()

        return ChatGenerationResult(
            text="".join(chunks),
            provider=route.provider,
            model=route.model,
            model_type_label=route.model_type_label,
        )
