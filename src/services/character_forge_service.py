"""Conversational character creation: session management and agent loop."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections import deque
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import openai

from src.core.config import settings
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError
from src.core.llm_registry import llm_registry
from src.db.models import (
    Character,
    CharacterForgeMessage,
    CharacterForgeSession,
    User,
)
from src.repositories.character_forge_repository import CharacterForgeRepository
from src.repositories.deepseek_search_repository import DeepSeekSearchRepository
from src.schemas.character_forge import (
    ForgeAudioRef,
    ForgeMessageItem,
    ForgeSessionDetail,
    ForgeSessionList,
    ForgeSessionSummary,
    ForgeSteerResult,
)
from src.schemas.chat import ChatImageRef
from src.services.character_forge_prompt import build_forge_system_prompt
from src.services.character_forge_tools import (
    ForgeToolExecutor,
    ToolContext,
    build_tool_schemas,
    collect_reference_images,
)
from src.services.character_service import CharacterService
from src.services.chat_service import ChatService, _build_user_message_with_images
from src.services.forge_preview_store import ForgePreviewStore
from src.services.llm_gateway_service import LLMGatewayService
from src.services.tts_gateway_service import TTSGatewayService

logger = logging.getLogger(__name__)

_WELCOME_MESSAGE = (
    "告诉我你想要一个什么样的角色。身份、性格、你和 TA 的关系，说一点就够了。"
)
_MAX_NAME_CHARS = 20


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _now() -> datetime:
    return datetime.now(UTC)


def _message_images(message: CharacterForgeMessage) -> list[ChatImageRef] | None:
    refs: list[ChatImageRef] = []
    for item in message.images or []:
        if not isinstance(item, dict):
            continue
        image_key = item.get("image_key")
        if not isinstance(image_key, str) or not image_key:
            continue
        refs.append(
            ChatImageRef(
                image_key=image_key,
                original=item.get("original") or "",
                display=item.get("display") or "",
                data_url=item.get("data_url"),
            )
        )
    return refs or None


def _message_to_llm(message: CharacterForgeMessage) -> dict[str, Any] | None:
    if message.role == "user":
        audios = _message_audios(message) or []
        text = message.content or ""
        if audios:
            text = f"{text}\n\n{_audio_marker(audios)}".strip()
        return _build_user_message_with_images(text, _message_images(message))
    if message.role == "assistant":
        payload: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or None,
        }
        if message.tool_calls:
            payload["tool_calls"] = message.tool_calls
        return payload
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or "",
            "content": message.content or "",
        }
    return None


def _message_audios(message: CharacterForgeMessage) -> list[ForgeAudioRef] | None:
    refs: list[ForgeAudioRef] = []
    for item in message.audios or []:
        if not isinstance(item, dict):
            continue
        audio_key = item.get("audio_key")
        if not isinstance(audio_key, str) or not audio_key:
            continue
        try:
            duration = float(item.get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        refs.append(
            ForgeAudioRef(
                audio_key=audio_key,
                name=str(item.get("name") or "")[:160],
                duration_seconds=max(0.0, duration),
            )
        )
    return refs or None


def _audio_marker(audios: list[ForgeAudioRef]) -> str:
    """Machine-readable hint so the model can address the recording by key."""

    lines = []
    for audio in audios:
        lines.append(
            f"- audio_key={audio.audio_key} | name={audio.name or '未命名录音'} "
            f"| duration={audio.duration_seconds:.1f}s"
        )
    return "[用户上传了录音]\n" + "\n".join(lines)


def _is_displayable(message: CharacterForgeMessage) -> bool:
    if message.role == "user":
        return bool(
            (message.content or "").strip() or message.audios or message.images
        )
    if message.role == "assistant":
        return bool((message.content or "").strip()) or bool(message.ui_payloads)
    return False


def _client_ui_payloads(payloads: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Strip server-side fields that the client does not need to render."""
    result: list[dict[str, Any]] = []
    for payload in payloads or []:
        payload = {
            key: value
            for key, value in payload.items()
            if key not in ("system_prompt", "voice_profile")
        }
        result.append(payload)
    return result


def _hydrate_created_payloads(
    payloads: list[dict[str, Any]],
    character: Character | None,
) -> list[dict[str, Any]]:
    """Keep persisted cards in sync with publish/unpublish changes."""
    if character is None:
        return payloads
    character_id = str(character.id)
    result: list[dict[str, Any]] = []
    for payload in payloads:
        if (
            payload.get("type") == "character_created"
            and payload.get("character_id") == character_id
        ):
            payload = {
                **payload,
                "visibility": character.visibility,
            }
        result.append(payload)
    return result


@dataclass
class _SteerItem:
    steer_id: str
    content: str


@dataclass
class _SessionRuntime:
    """Process-local state an active forge turn needs across HTTP requests.

    The streaming response and the steering request are separate requests, so
    the queue cannot live on a per-request service instance.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    steers: deque[_SteerItem] = field(default_factory=deque)
    active: bool = False


_SESSION_RUNTIMES: dict[uuid.UUID, _SessionRuntime] = {}


def _runtime_for(session_id: uuid.UUID) -> _SessionRuntime:
    runtime = _SESSION_RUNTIMES.get(session_id)
    if runtime is None:
        runtime = _SessionRuntime()
        _SESSION_RUNTIMES[session_id] = runtime
    return runtime


class CharacterForgeService:
    def __init__(
        self,
        *,
        repo: CharacterForgeRepository,
        tool_executor: ForgeToolExecutor,
        llm_gateway_service: LLMGatewayService,
        tts_gateway_service: TTSGatewayService,
        search_repository: DeepSeekSearchRepository,
        preview_store: ForgePreviewStore,
        character_service: CharacterService,
        chat_service: ChatService,
    ):
        self._repo = repo
        self._tools = tool_executor
        self._llm_gateway = llm_gateway_service
        self._tts = tts_gateway_service
        self._search = search_repository
        self._previews = preview_store
        self._characters = character_service
        self._chats = chat_service

    # ---------------------------------------------------------------- sessions

    async def _require_session(
        self, user: User, session_id: uuid.UUID
    ) -> CharacterForgeSession:
        session = await self._repo.get_session(session_id, user_id=user.id)
        if not session:
            raise APIError(
                status_code=404,
                code="resource_not_found",
                message=f"resource not found: forge session does not exist; session_id={session_id}",
            )
        return session

    async def create_session(self, user: User) -> ForgeSessionDetail:
        session = await self._repo.create_session(user_id=user.id)
        await self._repo.add_message(
            session_id=session.id,
            seq=1,
            role="assistant",
            content=_WELCOME_MESSAGE,
        )
        return await self.get_session_detail(user, session.id)

    async def rename_session(
        self, user: User, session_id: uuid.UUID, title: str
    ) -> ForgeSessionSummary:
        session = await self._require_session(user, session_id)
        cleaned = title.strip()
        if not cleaned:
            raise APIError(
                status_code=422,
                code="validation_error",
                message="validation error: title must not be blank",
            )
        await self._repo.touch_session(session, title=cleaned)
        return ForgeSessionSummary(
            id=str(session.id),
            title=session.title,
            updated_at=session.updated_at,
        )

    async def list_sessions(
        self, user: User, *, limit: int, cursor: datetime | None
    ) -> ForgeSessionList:
        sessions = await self._repo.list_sessions(
            user_id=user.id, limit=limit + 1, cursor=cursor
        )
        has_more = len(sessions) > limit
        page = sessions[:limit]

        character_ids = [s.active_character_id for s in page if s.active_character_id]
        characters = await self._repo.characters_by_ids(character_ids)

        items: list[ForgeSessionSummary] = []
        for session in page:
            character = characters.get(session.active_character_id) if session.active_character_id else None
            messages = await self._repo.list_messages(session.id, limit=200)
            last_text = None
            for message in reversed(messages):
                if _is_displayable(message):
                    last_text = (message.content or "").strip()[:60] or None
                    break
            items.append(
                ForgeSessionSummary(
                    id=str(session.id),
                    title=session.title,
                    updated_at=session.updated_at,
                    active_character_id=str(session.active_character_id)
                    if session.active_character_id
                    else None,
                    character_name=character.name if character else None,
                    character_avatar_url=None,
                    last_message=last_text,
                )
            )

        next_cursor = None
        if has_more and page:
            next_cursor = page[-1].updated_at.isoformat() if page[-1].updated_at else None
        return ForgeSessionList(items=items, next_cursor=next_cursor)

    async def get_session_detail(
        self, user: User, session_id: uuid.UUID
    ) -> ForgeSessionDetail:
        session = await self._require_session(user, session_id)
        messages = await self._repo.list_messages(session.id, limit=400)
        character = None
        if session.active_character_id:
            character = (
                await self._repo.characters_by_ids([session.active_character_id])
            ).get(session.active_character_id)

        return ForgeSessionDetail(
            session=ForgeSessionSummary(
                id=str(session.id),
                title=session.title,
                updated_at=session.updated_at,
                active_character_id=str(session.active_character_id)
                if session.active_character_id
                else None,
                character_name=character.name if character else None,
                character_avatar_url=None,
                last_message=None,
            ),
            items=[
                ForgeMessageItem(
                    id=str(message.id),
                    seq=message.seq,
                    role=message.role,
                    content=message.content,
                    ui_payloads=_hydrate_created_payloads(
                        _client_ui_payloads(message.ui_payloads), character
                    ),
                    images=_message_images(message) or [],
                    audios=_message_audios(message) or [],
                    created_at=message.created_at,
                )
                for message in messages
                if _is_displayable(message)
            ],
        )

    async def assert_stream_available(self, user: User, session_id: uuid.UUID) -> None:
        await self._require_session(user, session_id)
        if self._lock_for(session_id).locked():
            raise APIError(
                status_code=409,
                code="resource_conflict",
                message="resource conflict: this session already has a running reply",
            )

    async def steer(
        self,
        user: User,
        session_id: uuid.UUID,
        *,
        content: str,
        steer_id: str | None = None,
    ) -> ForgeSteerResult:
        """Queue a user message for the active turn.

        The message is not persisted yet: it becomes history only when the
        running loop reaches a provider-safe boundary (all tool results from
        the current batch already appended).
        """
        await self._require_session(user, session_id)

        runtime = _runtime_for(session_id)
        # No await between the check and the append, so the loop can only
        # change `active` before or after this block.
        if not runtime.active:
            raise APIError(
                status_code=409,
                code="resource_conflict",
                message="resource conflict: this session has no running reply to steer",
            )

        effective_id = steer_id or f"steer_{uuid.uuid4().hex}"
        runtime.steers.append(_SteerItem(steer_id=effective_id, content=content))
        logger.info(
            "character forge steer queued: session_id=%s steer_id=%s queue_length=%d",
            session_id,
            effective_id,
            len(runtime.steers),
        )
        return ForgeSteerResult(
            steer_id=effective_id,
            queue_length=len(runtime.steers),
        )

    async def delete_session(self, user: User, session_id: uuid.UUID) -> None:
        session = await self._require_session(user, session_id)
        await self._repo.delete_session(session)
        _SESSION_RUNTIMES.pop(session_id, None)

    # ------------------------------------------------------------------- agent

    def _lock_for(self, session_id: uuid.UUID) -> asyncio.Lock:
        return _runtime_for(session_id).lock

    async def stream_reply(
        self,
        user: User,
        session_id: uuid.UUID,
        content: str,
        images: list[ChatImageRef] | None = None,
        audios: list[ForgeAudioRef] | None = None,
    ) -> AsyncGenerator[str, None]:
        session = await self._require_session(user, session_id)

        runtime = _runtime_for(session_id)
        if runtime.lock.locked():
            raise APIError(
                status_code=409,
                code="resource_conflict",
                message="resource conflict: this session already has a running reply",
            )

        # `acquire()` is atomic with the check above in a single event loop, so
        # a second stream cannot slip in between.
        await runtime.lock.acquire()
        runtime.active = True
        runtime.steers.clear()
        try:
            async for chunk in self._stream_reply_locked(
                user, session, content, images, audios
            ):
                yield chunk
        finally:
            runtime.active = False
            runtime.steers.clear()
            runtime.lock.release()

    async def _stream_reply_locked(
        self,
        user: User,
        session: CharacterForgeSession,
        content: str,
        images: list[ChatImageRef] | None = None,
        audios: list[ForgeAudioRef] | None = None,
    ) -> AsyncGenerator[str, None]:
        route = llm_registry.resolve_chat_route()
        if route is None or not route.api_key:
            yield _sse(
                {
                    "type": "error",
                    "code": "llm_service_error",
                    "message": "upstream error: llm route is unavailable",
                }
            )
            return

        seq = await self._repo.next_seq(session.id)
        user_message = await self._repo.add_message(
            session_id=session.id,
            seq=seq,
            role="user",
            content=content,
            images=[image.model_dump() for image in images] if images else None,
            audios=[audio.model_dump() for audio in audios] if audios else None,
        )
        if session.title == "新角色":
            await self._repo.touch_session(
                session,
                title=content.strip()[:_MAX_NAME_CHARS] or "新角色",
            )

        yield _sse(
            {
                "type": "meta",
                "session_id": str(session.id),
                "user_message_id": str(user_message.id),
            }
        )

        persisted = await self._repo.list_messages(session.id, limit=400)
        history = persisted[-settings.CHARACTER_FORGE_HISTORY_LIMIT :]
        llm_messages: list[dict[str, Any]] = []
        loaded_skills: set[str] = set()
        for message in history:
            converted = _message_to_llm(message)
            if converted is not None:
                llm_messages.append(converted)
            if message.role != "tool" or message.tool_name != "load_skill":
                continue
            try:
                payload = json.loads(message.content or "{}")
            except json.JSONDecodeError:
                continue
            skill_name = payload.get("skill")
            if payload.get("ok") and isinstance(skill_name, str):
                loaded_skills.add(skill_name)

        active_character_payload = None
        if session.active_character_id:
            character = (
                await self._repo.characters_by_ids([session.active_character_id])
            ).get(session.active_character_id)
            if character:
                active_character_payload = {
                    "id": str(character.id),
                    "name": character.name,
                    "voice_name": character.voice_provider_voice_id,
                }

        system_prompt = build_forge_system_prompt(
            current_date=_now(),
            active_character=active_character_payload,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *llm_messages,
        ]
        reference_images = collect_reference_images(history, user_id=user.id)
        tools = build_tool_schemas(reference_image_keys=list(reference_images))
        tool_context = ToolContext(
            user=user,
            session=session,
            loaded_skills=loaded_skills,
            reference_images=reference_images,
        )
        search_budget: list[int] = []
        tool_context.search_calls = search_budget
        tool_context.avatar_calls = []

        runtime = _runtime_for(session.id)
        last_assistant_id: str | None = None
        step_index = 0
        step_limit = settings.CHARACTER_FORGE_MAX_STEPS
        try:
            while True:
                if step_index >= step_limit:
                    if not runtime.steers:
                        break
                    # Every steering message grants one more model request.
                    step_limit += 1

                if step_index > 0 and runtime.steers:
                    # Safe boundary: the previous assistant message and all of
                    # its tool results are already persisted and in `messages`.
                    drained = list(runtime.steers)
                    runtime.steers.clear()
                    step_limit += len(drained)
                    drained_payload: list[dict[str, str]] = []
                    for item in drained:
                        steer_seq = await self._repo.next_seq(session.id)
                        steer_message = await self._repo.add_message(
                            session_id=session.id,
                            seq=steer_seq,
                            role="user",
                            content=item.content,
                        )
                        messages.append({"role": "user", "content": item.content})
                        drained_payload.append(
                            {
                                "steer_id": item.steer_id,
                                "message_id": str(steer_message.id),
                                "content": item.content,
                            }
                        )
                    logger.info(
                        "character forge steer drained: session_id=%s steer_ids=%s",
                        session.id,
                        [item["steer_id"] for item in drained_payload],
                    )
                    yield _sse(
                        {"type": "steer_drained", "items": drained_payload}
                    )

                step_index += 1
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []

                async for event in self._llm_gateway.stream_turn_with_tools(
                    route=route,
                    messages=messages,
                    tools=tools,
                    operation="character_forge",
                    route_role="character_forge",
                    max_tokens=settings.CHARACTER_FORGE_MAX_OUTPUT_TOKENS,
                    reasoning_effort="max",
                    first_chunk_timeout_s=55.0,
                    user_id=str(user.id),
                    should_interrupt=lambda: bool(runtime.steers),
                ):
                    if event.get("type") == "text":
                        text_parts.append(event["text"])
                        yield _sse({"type": "chunk", "content": event["text"]})
                    elif event.get("type") == "reasoning":
                        yield _sse({"type": "reasoning", "content": event["text"]})
                    elif event.get("type") == "tool_calls":
                        tool_calls = event["calls"]

                assistant_text = "".join(text_parts).strip()
                assistant_message: CharacterForgeMessage | None = None
                if assistant_text or tool_calls:
                    assistant_seq = await self._repo.next_seq(session.id)
                    assistant_message = await self._repo.add_message(
                        session_id=session.id,
                        seq=assistant_seq,
                        role="assistant",
                        content=assistant_text or None,
                        tool_calls=[
                            {
                                "id": call.get("id") or f"call_{uuid.uuid4().hex}",
                                "type": "function",
                                "function": {
                                    "name": call.get("name") or "",
                                    "arguments": call.get("arguments") or "{}",
                                },
                            }
                            for call in tool_calls
                        ]
                        or None,
                    )
                    last_assistant_id = str(assistant_message.id)

                if not tool_calls:
                    if runtime.steers:
                        # The model finished its answer, but the user already
                        # steered: keep the turn alive instead of completing.
                        if assistant_text:
                            messages.append(
                                {"role": "assistant", "content": assistant_text}
                            )
                        continue
                    await self._repo.touch_session(session, updated_at=_now())
                    if runtime.steers:
                        if assistant_text:
                            messages.append(
                                {"role": "assistant", "content": assistant_text}
                            )
                        continue
                    yield _sse(
                        {"type": "done", "assistant_message_id": last_assistant_id}
                    )
                    return

                assert assistant_message is not None
                messages.append(
                    {
                        "role": "assistant",
                        "content": assistant_text or None,
                        "tool_calls": assistant_message.tool_calls,
                    }
                )

                for call in tool_calls:
                    call_id = call.get("id") or f"call_{uuid.uuid4().hex}"
                    name = call.get("name") or ""
                    raw_arguments = call.get("arguments") or "{}"
                    try:
                        arguments = json.loads(raw_arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                    yield _sse(
                        {
                            "type": "tool_started",
                            "tool": name,
                            "label": self._tool_label(name, arguments),
                        }
                    )
                    outcome = await self._tools.execute(name, arguments, tool_context)
                    if outcome.character_created is not None:
                        created = outcome.character_created
                        await self._repo.touch_session(
                            session,
                            title=created["name"],
                            active_character_id=uuid.UUID(created["character_id"]),
                        )
                    ok = bool(outcome.llm_result.get("ok"))
                    yield _sse(
                        {
                            "type": "tool_finished",
                            "tool": name,
                            "ok": ok,
                            "summary": self._tool_summary(name, outcome.llm_result),
                        }
                    )
                    if outcome.ui_payload:
                        # Persist before emitting the SSE event so a reload
                        # always finds the same card the live stream showed.
                        existing = list(assistant_message.ui_payloads or [])
                        await self._repo.update_message(
                            assistant_message,
                            ui_payloads=[*existing, outcome.ui_payload],
                        )
                        if outcome.ui_payload["type"] in {
                            "voice_preview",
                            "voice_cloned",
                            "character_created",
                            "character_updated",
                        }:
                            yield _sse(
                                {
                                    "type": outcome.ui_payload["type"],
                                    "payload": outcome.ui_payload,
                                }
                            )
                    tool_seq = await self._repo.next_seq(session.id)
                    await self._repo.add_message(
                        session_id=session.id,
                        seq=tool_seq,
                        role="tool",
                        content=json.dumps(
                            outcome.llm_result, ensure_ascii=False
                        ),
                        tool_call_id=call_id,
                        tool_name=name,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(
                                outcome.llm_result, ensure_ascii=False
                            ),
                        }
                    )

            await self._repo.touch_session(session, updated_at=_now())
            yield _sse({"type": "done", "assistant_message_id": last_assistant_id})
        except asyncio.CancelledError:
            raise
        except APIError as exc:
            discarded = self._discard_steers(session.id)
            if discarded:
                yield _sse(
                    {
                        "type": "steer_discarded",
                        "steer_ids": discarded,
                        "reason": "turn_failed",
                    }
                )
            yield _sse({"type": "error", "code": exc.code, "message": exc.message})
        except openai.RateLimitError as exc:
            logger.warning("character forge stream hit llm rate limit: %s", exc)
            discarded = self._discard_steers(session.id)
            if discarded:
                yield _sse(
                    {
                        "type": "steer_discarded",
                        "steer_ids": discarded,
                        "reason": "turn_failed",
                    }
                )
            yield _sse(
                {
                    "type": "error",
                    "code": ErrorCode.TOO_MANY_REQUESTS.value,
                    "message": "模型服务配额已用完（opencode Go 周配额），请更换 API Key 或稍后再试。",
                }
            )
        except Exception:
            logger.exception("character forge stream failed")
            discarded = self._discard_steers(session.id)
            if discarded:
                yield _sse(
                    {
                        "type": "steer_discarded",
                        "steer_ids": discarded,
                        "reason": "turn_failed",
                    }
                )
            yield _sse(
                {
                    "type": "error",
                    "code": "llm_service_error",
                    "message": "upstream error: character forge generation failed",
                }
            )

    @staticmethod
    def _discard_steers(session_id: uuid.UUID) -> list[str]:
        runtime = _runtime_for(session_id)
        discarded = [item.steer_id for item in runtime.steers]
        runtime.steers.clear()
        return discarded

    # ------------------------------------------------------------------ voice

    async def render_preview_audio(
        self,
        user: User,
        session_id: uuid.UUID,
        preview_id: str,
        audio_format: str,
    ) -> tuple[str, AsyncGenerator[bytes, None]]:
        payload = await self._previews.get(
            session_id=session_id,
            handle=preview_id,
            user_id=user.id,
        )
        stream = self._tts.stream_tts_audio_base64(
            text=str(payload.get("sample_text") or ""),
            audio_format=audio_format,
            provider="dashscope",
            provider_voice_id=str(payload.get("base_voice_id") or ""),
            provider_model=settings.DASHSCOPE_TTS_MODEL,
            voice_source_type="system",
            voice_profile=payload.get("voice_profile"),
        )
        try:
            first = await anext(stream)
        except StopAsyncIteration as exc:
            raise APIError(
                status_code=502,
                code="tts_upstream_error",
                message="upstream error: empty voice preview audio",
            ) from exc
        first_bytes = base64.b64decode(first[0])
        mime_type = first[1]

        async def generator() -> AsyncGenerator[bytes, None]:
            yield first_bytes
            async for chunk, _ in stream:
                yield base64.b64decode(chunk)

        return mime_type, generator()

    @staticmethod
    def _tool_label(name: str, arguments: dict[str, Any]) -> str:
        if name == "load_skill":
            skill_name = str(arguments.get("name") or "").strip()
            display = {
                "prompt-creation": "人设提示词",
                "voice-creation": "音色创建",
                "voice-clone": "音色克隆",
                "avatar-creation": "头像生成",
            }.get(skill_name, "技能")
            return f"正在加载{display}技能"
        if name == "preview_voice":
            return "正在调配音色"
        if name == "generate_avatar":
            return "正在生成角色形象"
        if name == "create_character":
            return "正在创建角色"
        if name == "update_character":
            return "正在修改角色"
        if name == "search_web":
            query = str(arguments.get("query") or "")[:30]
            return f"正在搜索：{query}" if query else "正在搜索"
        if name == "read_webpage":
            return "正在读取网页"
        if name == "inspect_audio":
            return "正在检查录音"
        if name == "crop_audio":
            return "正在裁剪录音"
        if name == "clone_voice":
            return "正在克隆音色"
        return "正在处理"

    @staticmethod
    def _tool_summary(name: str, result: dict[str, Any]) -> str:
        if not result.get("ok"):
            errors = result.get("errors") or []
            return str(errors[0])[:80] if errors else "执行失败"
        if name == "load_skill":
            return "技能已加载"
        if name == "preview_voice":
            return "试听已生成"
        if name == "generate_avatar":
            return "形象已生成"
        if name == "create_character":
            return "角色已创建"
        if name == "update_character":
            return "角色已更新"
        if name == "search_web":
            return f"找到 {len(result.get('results') or [])} 条结果"
        if name == "read_webpage":
            return "网页已读取"
        if name == "inspect_audio":
            duration = result.get("duration_seconds")
            return f"录音 {duration} 秒" if duration else "录音已检查"
        if name == "crop_audio":
            duration = result.get("duration_seconds")
            if result.get("cropped"):
                return f"已裁到 {duration} 秒"
            return f"{duration} 秒 · 无需裁剪"
        if name == "clone_voice":
            return "音色已生成"
        return "完成"
