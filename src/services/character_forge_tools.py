"""Tool schemas and execution for the character-forge agent."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from src.core.config import settings
from src.core.exceptions import APIError
from src.core.tts_registry import DEFAULT_SYSTEM_TTS_PROVIDER
from src.core.tts_voice_catalog import dashscope_full_voice_catalog
from src.core.voice_profiles import normalize_voice_profile
from src.db.models import (
    CharacterForgeMessage,
    CharacterForgeSession,
    User,
)
from src.repositories.deepseek_search_repository import DeepSeekSearchRepository
from src.schemas.character import CharacterCreate, CharacterUpdate
from src.schemas.media import MediaUploadKind
from src.services.audio_media_service import AudioMediaService
from src.services.avatar_generation_service import AvatarGenerationService
from src.services.character_service import CharacterService
from src.services.chat_service import ChatService
from src.services.forge_preview_store import ForgePreviewStore
from src.services.forge_skills import SKILL_NAMES, load_forge_skill
from src.services.media_url_service import (
    build_r2_public_url,
    original_object_key,
    validate_avatar_image_key_owner,
    validate_chat_audio_key_owner,
)
from src.services.voice_profile_service import VoiceProfileService

logger = logging.getLogger(__name__)

_MAX_SAMPLE_CHARS = 200
_MIN_SAMPLE_CHARS = 10
_MAX_REFERENCE_IMAGES = 3
_DEFAULT_CLONE_PREVIEW_TEXT = (
    "Hi, this is my cloned voice. How does it sound?"
)

# Guidance the agent reads after an upstream refusal. The upstream reason
# itself is prepended by ``_upstream_failure_errors``; these strings only
# say what the agent can do next.
_AVATAR_FAILURE_GUIDANCE: dict[str, str] = {
    "DataInspectionFailed": (
        "参考图被上游内容审核拒绝，含清晰可识别的真人面部最常见。"
        "请改用纯文字描述重新生成（不要传 reference_image_keys），"
        "或者告诉用户这张参考图用不了，请他换一张。"
    ),
}
_AVATAR_FAILURE_FALLBACK = (
    "请调整描述或参考图再试一次；连续失败就告诉用户头像暂时生成不了，"
    "先不带头像继续创建角色。"
)
_SEARCH_FAILURE_FALLBACK = "请换一个关键词或来源再搜一次，或基于已有信息继续。"
_FETCH_FAILURE_FALLBACK = "这个网址可能抓不到内容，请换一个来源再试，或基于已有信息继续。"

VOICE_CATALOG = {item.provider_voice_id: item for item in dashscope_full_voice_catalog()}


def _voice_ids() -> list[str]:
    return list(VOICE_CATALOG.keys())


def collect_reference_images(
    messages: list[CharacterForgeMessage], *, user_id: uuid.UUID
) -> dict[str, str]:
    """Map user-attached chat image keys to a source Qwen-Image can read.

    Inline ``data:`` URLs are preferred: they work even when R2 has no public
    base URL configured. Older attachments fall back to the public original
    object URL. Keys owned by another user are never exposed to the model.
    """
    images: dict[str, str] = {}
    for message in messages:
        if message.role != "user":
            continue
        for item in message.images or []:
            if not isinstance(item, dict):
                continue
            image_key = item.get("image_key")
            if not isinstance(image_key, str):
                continue
            normalized = validate_avatar_image_key_owner(
                image_key,
                owner_id=str(user_id),
                allowed_kinds=(MediaUploadKind.CHAT_IMAGE,),
            )
            if normalized is None:
                continue
            data_url = item.get("data_url")
            if isinstance(data_url, str) and data_url.startswith("data:image/"):
                images[normalized] = data_url
                continue
            public_url = build_r2_public_url(original_object_key(normalized))
            if public_url:
                images[normalized] = public_url
    # Keep the most recent attachments within the model's three-image limit.
    return dict(list(images.items())[-_MAX_REFERENCE_IMAGES:])


def _voice_binding_properties(voice_id_schema: dict[str, Any]) -> dict[str, Any]:
    """Schema of the voice fields a character tool call carries.

    These are the values the agent already chose for the audition, so the
    server can check every one of them instead of trusting a signed blob.
    """
    return {
        "voice_base_id": {
            **voice_id_schema,
            "description": "基础音色 id，取试听时选定的那个",
        },
        "voice_speech_rate": {
            "type": "number",
            "minimum": 0.75,
            "maximum": 1.35,
            "description": "语速。0.88 慢条斯理 / 0.95 从容 / 1.0 正常 / 1.08 利落 / 1.18 急促",
        },
        "voice_volume": {
            "type": "integer",
            "minimum": 20,
            "maximum": 80,
            "description": "音量。35 轻 / 50 正常 / 65 足",
        },
        "voice_pitch_rate": {
            "type": "number",
            "minimum": 0.85,
            "maximum": 1.15,
            "description": "音调。0.95 略低 / 1.0 自然 / 1.05 略高。调低会同时变慢",
        },
        "voice_profile_id": {
            "type": "string",
            "description": (
                "clone_voice 返回的 voice_profile_id。"
                "用刚克隆的音色建角色时填这个，不需要再填 voice_base_id。"
            ),
        },
    }


def build_tool_schemas(
    *, reference_image_keys: list[str] | None = None
) -> list[dict[str, Any]]:
    voice_id_schema = {"type": "string", "enum": _voice_ids()}
    voice_binding_properties = _voice_binding_properties(voice_id_schema)
    reference_property: dict[str, Any] = {}
    if reference_image_keys:
        reference_property = {
            "reference_image_keys": {
                "type": "array",
                "items": {"type": "string", "enum": reference_image_keys},
                "maxItems": _MAX_REFERENCE_IMAGES,
                "description": (
                    "用户在对话里上传、想作为头像参考图的 image_key，按顺序最多 3 张；"
                    "不传则纯文生图。只能使用这里列出的值。"
                ),
            }
        }
    return [
        {
            "type": "function",
            "function": {
                "name": "load_skill",
                "description": (
                    "加载对应技能的完整技能说明。"
                    "设计角色人设、编写 system_prompt 前必须先加载 prompt-creation；"
                    "选择音色、调整参数或生成试听前必须先加载 voice-creation；"
                    "克隆用户自己的声音前必须先加载 voice-clone；"
                    "生成或更换头像前必须先加载 avatar-creation。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": list(SKILL_NAMES)},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "preview_voice",
                "description": (
                    "为角色生成一段音色试听。选择一个基础系统音色，并设置语速、音量、音调，"
                    "返回试听编号和可播放的试听地址。"
                    "创建角色时把这里用的 base_voice_id 和三个参数原样填进 create_character。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "base_voice_id": voice_id_schema,
                        "speech_rate": {
                            "type": "number",
                            "minimum": 0.75,
                            "maximum": 1.35,
                            "description": "语速。0.88 慢条斯理 / 0.95 从容 / 1.0 正常 / 1.08 利落 / 1.18 急促",
                        },
                        "volume": {
                            "type": "integer",
                            "minimum": 20,
                            "maximum": 80,
                            "description": "音量。35 轻 / 50 正常 / 65 足",
                        },
                        "pitch_rate": {
                            "type": "number",
                            "minimum": 0.85,
                            "maximum": 1.15,
                            "description": "音调。0.95 略低 / 1.0 自然 / 1.05 略高。调低会同时变慢",
                        },
                        "sample_text": {
                            "type": "string",
                            "minLength": _MIN_SAMPLE_CHARS,
                            "maxLength": _MAX_SAMPLE_CHARS,
                            "description": "最能体现角色说话方式的一句台词，全英文",
                        },
                    },
                    "required": [
                        "base_voice_id",
                        "speech_rate",
                        "volume",
                        "pitch_rate",
                        "sample_text",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_avatar",
                "description": (
                    "根据角色的外貌描述生成一张头像（qwen-image-3.0，2048x2048 最高质量）。"
                    "角色创建完成、用户同意做头像之后调用。"
                    + (
                        "用户在本会话上传过参考图，可用 image_key："
                        + "、".join(reference_image_keys)
                        + "；用户想用参考图时把这些 key 传进 reference_image_keys，"
                        "模型会参考图中的五官、气质或画风；用户没提参考图就不传。"
                        if reference_image_keys
                        else "本会话没有可用的参考图；prompt 写纯文生图描述即可。"
                    )
                    + "生成后把返回的 avatar_image_key 传给 update_character。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "minLength": 20,
                            "maxLength": 1500,
                            "description": (
                                "头像画面描述：构图、年龄性别、发型、服装、表情、光线、背景、风格。"
                                "不要出现文字、水印或多人。"
                            ),
                        },
                        **reference_property,
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_character",
                "description": (
                    "直接创建角色并展示角色卡片（可对话、生成头像、上架到市场）。"
                    "调用后角色立即创建，不需要用户再点确认。"
                    "当前会话已经有角色时禁止调用，应改用 update_character。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 20,
                            "description": "角色名，全英文",
                        },
                        "description": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 35,
                            "description": "一句话身份钩子，全英文",
                        },
                        "greeting_message": {"type": "string", "maxLength": 200},
                        "system_prompt": {
                            "type": "string",
                            "minLength": 200,
                            "maxLength": 20000,
                        },
                        **voice_binding_properties,
                        "avatar_image_key": {
                            "type": "string",
                            "description": "generate_avatar 返回的 avatar_image_key",
                        },
                    },
                    "required": [
                        "name",
                        "description",
                        "system_prompt",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_character",
                "description": (
                    "修改当前会话已创建的角色。只传需要改变的字段。"
                    "换声音时传新的 voice_base_id 和三个音色参数；"
                    "角色 id 默认取当前会话的角色。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "character_id": {"type": "string"},
                        "name": {"type": "string", "minLength": 1, "maxLength": 20},
                        "description": {"type": "string", "minLength": 1, "maxLength": 35},
                        "greeting_message": {"type": "string", "maxLength": 200},
                        "system_prompt": {
                            "type": "string",
                            "minLength": 200,
                            "maxLength": 20000,
                        },
                        **voice_binding_properties,
                        "avatar_image_key": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "用 DeepSeek 做网络搜索，返回标题、URL 和摘要片段。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": 300},
                        "num_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 8,
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_webpage",
                "description": "抓取一个网页的正文，最多返回 8000 字符。",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "inspect_audio",
                "description": (
                    "检查用户上传的录音：时长、采样率、音量水平、最长停顿多长、"
                    "以及建议从第几秒开始截取。克隆音色之前先调用它，再决定怎么裁。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "audio_key": {
                            "type": "string",
                            "description": "用户消息里 [用户上传了录音] 标记中的 audio_key",
                        },
                        "target_seconds": {
                            "type": "number",
                            "description": "计划截取的秒数，用来计算建议起点，默认 10",
                            "minimum": 3,
                            "maximum": 30,
                        },
                    },
                    "required": ["audio_key"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "crop_audio",
                "description": (
                    "从录音里截出一段干净片段用于克隆。不传 start_seconds 用建议值，"
                    "不传 duration_seconds 用 10 秒。返回新的 audio_key，交给 clone_voice 使用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "audio_key": {"type": "string"},
                        "start_seconds": {
                            "type": "number",
                            "minimum": 0,
                            "description": "从第几秒开始，不传则自动挑最长停顿之后的起点",
                        },
                        "duration_seconds": {
                            "type": "number",
                            "minimum": 3,
                            "maximum": 30,
                            "description": "截多长，默认 10 秒",
                        },
                    },
                    "required": ["audio_key"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "clone_voice",
                "description": (
                    "用一段录音克隆出专属音色。调用前先加载 voice-clone 技能。"
                    "audio_key 用 crop_audio 返回的新 key。"
                    "成功后返回 voice_profile_id，可以交给 create_character 绑定。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "audio_key": {"type": "string"},
                        "display_name": {
                            "type": "string",
                            "description": (
                                "音色名字，必须用英文（2-4 个单词），"
                                "例如 Warm Narrator、My Voice"
                            ),
                            "minLength": 1,
                            "maxLength": 40,
                        },
                        "preview_text": {
                            "type": "string",
                            "description": (
                                "试听时念的英文文案，最多 120 字符，不传用默认英文短句"
                            ),
                            "maxLength": 120,
                        },
                        "language_hint": {
                            "type": "string",
                            "description": "录音主要语言，zh 或 en；不确定就不传",
                        },
                    },
                    "required": ["audio_key", "display_name"],
                    "additionalProperties": False,
                },
            },
        },
    ]


@dataclass
class ToolOutcome:
    llm_result: dict[str, Any]
    ui_payload: dict[str, Any] | None = None
    character_created: dict[str, Any] | None = None


@dataclass
class ToolContext:
    user: User
    session: CharacterForgeSession
    loaded_skills: set[str] = field(default_factory=set)
    reference_images: dict[str, str] = field(default_factory=dict)


def _require_skill(context: ToolContext, name: str) -> ToolOutcome | None:
    if name in context.loaded_skills:
        return None
    return _field_error(
        [f"请先调用 load_skill 加载 {name} 技能，再执行这个操作。"]
    )


def _voice_summary(voice_id: str) -> dict[str, Any]:
    item = VOICE_CATALOG.get(voice_id)
    if item is None:
        # Cloned voices carry a provider voice id that is not in the system catalog.
        return {
            "provider_voice_id": voice_id,
            "display_name": None,
            "usage_hint": None,
            "gender": None,
        }
    return {
        "provider_voice_id": item.provider_voice_id,
        "display_name": item.display_name,
        "usage_hint": item.usage_hint,
        "gender": item.gender,
    }


def _field_error(errors: list[str]) -> ToolOutcome:
    return ToolOutcome(llm_result={"ok": False, "errors": errors})


_MAX_MODEL_ERROR_CHARS = 10_000


def _truncate_model_error(text: str) -> str:
    """Keep long upstream errors readable for the model without unbounded input."""

    if len(text) <= _MAX_MODEL_ERROR_CHARS:
        return text
    half = _MAX_MODEL_ERROR_CHARS // 2
    return f"{text[:half]}\n...[truncated]...\n{text[-half:]}"


def _exception_chain_messages(exc: BaseException) -> list[str]:
    """Collect the original messages from an exception and its cause chain."""

    messages: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip()
        if text and text not in messages:
            messages.append(text)
        current = current.__cause__ or current.__context__
    return messages


def _tool_error_model_content(
    exc: BaseException,
    *,
    guidance: str | None = None,
) -> str:
    """Raw error detail for the agent only; never rendered in the user UI.

    Mirrors ZCode's tool error contract: ``errors`` is the short channel shown
    in the tool chip, while ``model_content`` keeps the upstream/cause text so
    the agent can explain and recover from the real failure.
    """

    lines: list[str] = []
    if isinstance(exc, APIError):
        lines.extend(
            [
                "type=APIError",
                f"code={exc.code}",
                f"status={exc.status_code}",
                f"message={exc.message}",
            ]
        )
        if exc.upstream_code:
            lines.append(f"upstream_code={exc.upstream_code}")
        if exc.upstream_message:
            lines.append(f"upstream_message={exc.upstream_message}")
        causes = _exception_chain_messages(exc)[1:]
    else:
        lines.append(f"type={type(exc).__name__}")
        causes = _exception_chain_messages(exc)

    for cause in causes:
        lines.append(f"cause={cause}")
    if guidance:
        lines.append(f"guidance={guidance}")

    body = _truncate_model_error("\n".join(lines))
    return f"<tool_use_error>\n{body}\n</tool_use_error>"


def _upstream_failure_result(
    display_message: str,
    exc: APIError,
    *,
    guidance: dict[str, str] | None = None,
    fallback: str,
) -> dict[str, Any]:
    """Build the tool result for an upstream refusal.

    The result carries two channels, the same split ZCode uses for tool
    errors. ``errors`` keeps the message the user already sees in the tool
    chip, unchanged. ``model_content`` carries the original upstream reason:
    only the model reads it, and it is what lets the agent explain what
    actually failed instead of guessing at it.
    """
    hint = fallback
    if guidance:
        hint = guidance.get(exc.upstream_code or "") or fallback
    return {
        "ok": False,
        "errors": [display_message],
        "model_content": _tool_error_model_content(exc, guidance=hint),
    }


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_english_text(value: str) -> bool:
    """Cloned voice names and preview lines are English-only."""

    letters = [char for char in value if not char.isspace()]
    if not letters:
        return False
    return all(ord(char) < 128 for char in letters)


@dataclass(frozen=True)
class ResolvedVoice:
    """The voice a character should be bound to."""

    base_voice_id: str
    profile: dict[str, Any]
    provider: str = DEFAULT_SYSTEM_TTS_PROVIDER
    model: str | None = None
    source_type: str = "system"


def _resolve_voice_arguments(
    arguments: dict[str, Any],
    *,
    required: bool,
    base_profile: dict[str, Any] | None = None,
) -> tuple[ResolvedVoice | None, ToolOutcome | None]:
    """Read the voice fields a character tool call carries.

    The agent passes the base voice id and the three shaping values it already
    picked for the audition. Those are short and verifiable on the server; an
    opaque preview token is not, because a model re-types prose it has to copy
    and the signature stops matching.

    ``base_profile`` supplies the values the caller left out, so a partial
    voice change keeps the shaping the character already has.
    """
    base_voice_id = str(arguments.get("voice_base_id") or "").strip()
    provided: dict[str, Any] = {}
    for field_name, argument_name in (
        ("speech_rate", "voice_speech_rate"),
        ("volume", "voice_volume"),
        ("pitch_rate", "voice_pitch_rate"),
    ):
        value = arguments.get(argument_name)
        if value is not None:
            provided[field_name] = value

    if not base_voice_id:
        if provided:
            return None, _field_error(
                ["缺少 voice_base_id。只改语速/音量/音调时也要带上基础音色 id。"]
            )
        if required:
            return None, _field_error(
                ["缺少 voice_base_id。请从音色目录里选一个基础音色。"]
            )
        return None, None

    if base_voice_id not in VOICE_CATALOG:
        return None, _field_error(
            [f"voice_base_id 不在音色目录中：{base_voice_id}。请从给定列表里选。"]
        )

    raw_profile = {
        key: value
        for key, value in (base_profile or {}).items()
        if key in ("speech_rate", "volume", "pitch_rate")
    }
    raw_profile.update(provided)
    raw_profile["base_voice_id"] = base_voice_id
    try:
        profile = normalize_voice_profile(raw_profile)
    except APIError as exc:
        return None, _field_error([exc.message])
    return ResolvedVoice(base_voice_id=base_voice_id, profile=profile.to_dict()), None


class ForgeToolExecutor:
    def __init__(
        self,
        *,
        character_service: CharacterService,
        chat_service: ChatService,
        search_repository: DeepSeekSearchRepository,
        preview_store: ForgePreviewStore,
        avatar_service: AvatarGenerationService,
        audio_service: AudioMediaService,
        voice_profile_service: VoiceProfileService,
    ):
        self._character_service = character_service
        self._chat_service = chat_service
        self._search = search_repository
        self._previews = preview_store
        self._avatars = avatar_service
        self._audios = audio_service
        self._voices = voice_profile_service

    async def execute(
        self, tool_name: str, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        handlers = {
            "load_skill": self._load_skill,
            "preview_voice": self._preview_voice,
            "generate_avatar": self._generate_avatar,
            "create_character": self._create_character,
            "update_character": self._update_character,
            "search_web": self._search_web,
            "read_webpage": self._read_webpage,
            "inspect_audio": self._inspect_audio,
            "crop_audio": self._crop_audio,
            "clone_voice": self._clone_voice,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return _field_error([f"未知工具：{tool_name}"])
        try:
            return await handler(arguments, context)
        except asyncio.CancelledError:
            raise
        except APIError as exc:
            logger.warning(
                "Character forge tool failed: tool=%s code=%s status=%s message=%s",
                tool_name,
                exc.code,
                exc.status_code,
                exc.message,
            )
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": [exc.message],
                    "model_content": _tool_error_model_content(exc),
                }
            )
        except ValidationError as exc:
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": ["工具参数校验失败，请调整参数后重试。"],
                    "model_content": _tool_error_model_content(
                        exc,
                        guidance="这是字段校验详情，请按字段修正参数后重试。",
                    ),
                }
            )
        except Exception as exc:
            logger.exception(
                "Character forge tool crashed: tool=%s error_type=%s",
                tool_name,
                type(exc).__name__,
            )
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": ["工具执行失败，请调整参数或换一种做法后重试。"],
                    "model_content": _tool_error_model_content(exc),
                }
            )

    async def _load_skill(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        name = str(arguments.get("name") or "").strip()
        skill = load_forge_skill(name)
        if skill is None:
            return _field_error(
                [
                    f"未知技能：{name}。可选技能：{', '.join(SKILL_NAMES)}。"
                ]
            )
        context.loaded_skills.add(skill.name)
        return ToolOutcome(
            llm_result={
                "ok": True,
                "skill": skill.name,
                "content": skill.content,
                "note": "技能已加载。按技能说明继续，不要向用户复述技能内容。",
            }
        )

    async def _preview_voice(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        missing = _require_skill(context, "voice-creation")
        if missing is not None:
            return missing
        base_voice_id = str(arguments.get("base_voice_id") or "").strip()
        if base_voice_id not in VOICE_CATALOG:
            return _field_error(
                [f"base_voice_id 不在音色目录中：{base_voice_id}。请从给定列表里选。"]
            )

        sample_text = str(arguments.get("sample_text") or "").strip()
        if not (_MIN_SAMPLE_CHARS <= len(sample_text) <= _MAX_SAMPLE_CHARS):
            return _field_error(
                [f"sample_text 需要 {_MIN_SAMPLE_CHARS}-{_MAX_SAMPLE_CHARS} 个字符"]
            )

        profile = normalize_voice_profile(
            {
                "base_voice_id": base_voice_id,
                "speech_rate": arguments.get("speech_rate"),
                "volume": arguments.get("volume"),
                "pitch_rate": arguments.get("pitch_rate"),
            }
        )
        preview_id = await self._previews.create(
            session_id=context.session.id,
            user_id=context.user.id,
            payload={
                "base_voice_id": base_voice_id,
                "voice_profile": profile.to_dict(),
                "sample_text": sample_text,
            },
        )
        voice = _voice_summary(base_voice_id)
        preview_url = (
            f"/v1/character-forge/voice-previews/{context.session.id}/{preview_id}/audio"
        )
        ui_payload = {
            "type": "voice_preview",
            "preview_id": preview_id,
            "preview_url": preview_url,
            "base_voice_id": base_voice_id,
            "display_name": voice["display_name"],
            "usage_hint": voice["usage_hint"],
            "gender": voice["gender"],
            "speech_rate": profile.speech_rate,
            "volume": profile.volume,
            "pitch_rate": profile.pitch_rate,
            "sample_text": sample_text,
        }
        return ToolOutcome(
            llm_result={
                "ok": True,
                "preview_id": preview_id,
                "voice": voice,
                "speech_rate": profile.speech_rate,
                "volume": profile.volume,
                "pitch_rate": profile.pitch_rate,
                "note": (
                    "试听已生成并展示给用户。继续调用 create_character，"
                    "把这里选的 base_voice_id 和三个参数原样填进去。"
                ),
            },
            ui_payload=ui_payload,
        )

    async def _resolve_voice_for_character(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
        *,
        required: bool,
        base_profile: dict[str, Any] | None = None,
    ) -> tuple[ResolvedVoice | None, ToolOutcome | None]:
        """Prefer a cloned voice when the agent passes ``voice_profile_id``."""

        raw_profile_id = str(arguments.get("voice_profile_id") or "").strip()
        if raw_profile_id:
            try:
                voice_uuid = uuid.UUID(raw_profile_id)
            except ValueError:
                return None, _field_error(
                    ["voice_profile_id 不是合法的 id，请使用 clone_voice 返回的值。"]
                )
            profile = await self._voices.get_voice_by_id(
                voice_id=voice_uuid, current_user=context.user
            )
            shaping = normalize_voice_profile(
                {
                    key: value
                    for key, value in (base_profile or {}).items()
                    if key in ("speech_rate", "volume", "pitch_rate")
                }
            )
            return (
                ResolvedVoice(
                    base_voice_id=profile.provider_voice_id,
                    profile=shaping.to_dict(),
                    provider=profile.provider,
                    model=profile.provider_model,
                    source_type="clone",
                ),
                None,
            )

        return _resolve_voice_arguments(
            arguments, required=required, base_profile=base_profile
        )

    def _resolve_audio_key(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> tuple[str, ToolOutcome | None]:
        raw = str(arguments.get("audio_key") or "").strip()
        audio_key = validate_chat_audio_key_owner(raw, owner_id=str(context.user.id))
        if audio_key is None:
            return "", _field_error(
                ["audio_key 无效或不属于当前用户。请使用用户消息里给出的 audio_key。"]
            )
        return audio_key, None

    async def _inspect_audio(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        audio_key, error = self._resolve_audio_key(arguments, context)
        if error is not None:
            return error

        fmt = AudioMediaService.format_from_key(audio_key)
        body = await self._audios.load_chat_audio(audio_key)
        target = float(
            arguments.get("target_seconds") or settings.VOICE_CLONE_MAX_PROMPT_SECONDS
        )
        analysis = await self._audios.analyze(body, fmt, target_seconds=target)
        return ToolOutcome(
            llm_result={
                "ok": True,
                "audio_key": audio_key,
                "duration_seconds": round(analysis.duration_seconds, 2),
                "loudness_db": round(analysis.rms_db_median, 1),
                "quiet_ratio": round(analysis.quiet_ratio, 3),
                "longest_pause_seconds": round(analysis.longest_quiet_gap_seconds, 2),
                "suggested_start_seconds": round(analysis.suggested_start_seconds, 2),
                "suggested_duration_seconds": round(
                    analysis.suggested_duration_seconds, 2
                ),
                "note": (
                    "裁剪时调用 crop_audio；不传参数就用这里的建议值。"
                    "录音里有明显停顿就优先后半段，选择权在你。"
                ),
            }
        )

    async def _crop_audio(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        audio_key, error = self._resolve_audio_key(arguments, context)
        if error is not None:
            return error

        fmt = AudioMediaService.format_from_key(audio_key)
        body = await self._audios.load_chat_audio(audio_key)
        cropped = await self._audios.crop(
            body,
            fmt,
            start_seconds=_optional_float(arguments.get("start_seconds")),
            duration_seconds=_optional_float(arguments.get("duration_seconds")),
        )
        stored = await self._audios.store_chat_audio(
            owner_id=context.user.id,
            body=cropped.body,
            filename=f"clip.{cropped.format}",
            content_type=cropped.content_type,
        )
        return ToolOutcome(
            llm_result={
                "ok": True,
                "audio_key": stored.audio_key,
                "duration_seconds": round(stored.duration_seconds, 2),
                "cropped": cropped.cropped,
                "note": "把返回的 audio_key 交给 clone_voice。",
            }
        )

    async def _clone_voice(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        missing = _require_skill(context, "voice-clone")
        if missing is not None:
            return missing

        audio_key, error = self._resolve_audio_key(arguments, context)
        if error is not None:
            return error

        display_name = str(arguments.get("display_name") or "").strip()
        if not display_name:
            return _field_error(["display_name 不能为空。"])
        if not _is_english_text(display_name):
            return _field_error(
                ["display_name 必须是英文，例如 Warm Narrator。请换一个英文名字再调用。"]
            )
        preview_text = (
            str(arguments.get("preview_text") or "").strip()
            or _DEFAULT_CLONE_PREVIEW_TEXT
        )
        if not _is_english_text(preview_text):
            # 试听文案对用户不可见地回退到默认英文句，避免多绕一轮工具调用。
            preview_text = _DEFAULT_CLONE_PREVIEW_TEXT
        language_hint = str(arguments.get("language_hint") or "").strip() or None

        fmt = AudioMediaService.format_from_key(audio_key)
        body = await self._audios.load_chat_audio(audio_key)
        info = await self._audios.probe_info(body, fmt)
        try:
            accepted = await self._voices.create_clone(
                current_user=context.user,
                provider="dashscope",
                display_name=display_name,
                preview_text=preview_text,
                source_audio_bytes=body,
                source_audio_format=fmt,
                description=None,
                language_hint=language_hint,
                idempotency_key=None,
            )
        except APIError as exc:
            if exc.status_code >= 500:
                logger.warning("voice clone upstream failed: %s", exc.message)
                return ToolOutcome(
                    llm_result=_upstream_failure_result(
                        "音色服务暂时不可用，请稍后再试。",
                        exc,
                        fallback=(
                            "不要再重试这一次调用，直接用一句人话告诉用户稍后再试，"
                            "或者先用系统音色。"
                        ),
                    )
                )
            raise
        profile = accepted.voice
        preview_url = f"/v1/voices/{profile.id}/preview/audio?audio_format=mp3"
        ui_payload = {
            "type": "voice_cloned",
            "voice_profile_id": profile.id,
            "display_name": profile.display_name,
            "sample_text": profile.preview_text or preview_text,
            "preview_url": preview_url,
            "duration_seconds": round(info.duration_seconds, 1),
        }
        return ToolOutcome(
            llm_result={
                "ok": True,
                "voice_profile_id": profile.id,
                "display_name": profile.display_name,
                "sample_text": ui_payload["sample_text"],
                "note": (
                    "音色卡已经展示给用户。接着问用户要不要用这个音色建角色；"
                    "要的话调 create_character 并带上 voice_profile_id。"
                ),
            },
            ui_payload=ui_payload,
        )

    async def _generate_avatar(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        missing = _require_skill(context, "avatar-creation")
        if missing is not None:
            return missing
        prompt = str(arguments.get("prompt") or "").strip()
        if not (20 <= len(prompt) <= 1500):
            return _field_error(["prompt 需要 20-1500 个字符"])

        raw_keys = arguments.get("reference_image_keys") or []
        if not isinstance(raw_keys, list):
            return _field_error(["reference_image_keys 必须是数组"])
        if len(raw_keys) > _MAX_REFERENCE_IMAGES:
            return _field_error([f"参考图最多 {_MAX_REFERENCE_IMAGES} 张"])
        reference_images: list[str] = []
        for key in raw_keys:
            if not isinstance(key, str) or key not in context.reference_images:
                available = ", ".join(context.reference_images) or "无"
                return _field_error(
                    [f"参考图 {key} 不可用；可用 image_key：{available}"]
                )
            if context.reference_images[key] not in reference_images:
                reference_images.append(context.reference_images[key])

        try:
            image_key, _avatar_urls = await self._avatars.generate(
                user_id=context.user.id,
                prompt=prompt,
                reference_images=reference_images or None,
            )
        except APIError as exc:
            logger.warning("generate_avatar failed: %s", exc)
            return ToolOutcome(
                llm_result=_upstream_failure_result(
                    exc.message,
                    exc,
                    guidance=_AVATAR_FAILURE_GUIDANCE,
                    fallback=_AVATAR_FAILURE_FALLBACK,
                )
            )
        return ToolOutcome(
            llm_result={
                "ok": True,
                "avatar_image_key": image_key,
                "note": "头像已生成。把 avatar_image_key 传给 update_character 绑定当前角色。",
            },
            ui_payload=None,
        )

    async def _create_character(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        missing = _require_skill(context, "prompt-creation")
        if missing is not None:
            return missing
        if context.session.active_character_id:
            return _field_error(
                ["当前会话已经有角色。请调用 update_character 修改，不要重复创建。"]
            )

        voice, voice_error = await self._resolve_voice_for_character(
            arguments, context, required=True
        )
        if voice_error is not None:
            return voice_error
        assert voice is not None

        name = str(arguments.get("name") or "").strip()
        description = str(arguments.get("description") or "").strip()
        system_prompt = str(arguments.get("system_prompt") or "").strip()
        greeting_message = str(arguments.get("greeting_message") or "").strip() or None
        errors: list[str] = []
        if not (1 <= len(name) <= 20):
            errors.append(f"name 需要 1-20 个字符（当前 {len(name)}）")
        if not (1 <= len(description) <= 35):
            errors.append(f"description 需要 1-35 个字符（当前 {len(description)}）")
        if not (200 <= len(system_prompt) <= 20000):
            errors.append(f"system_prompt 需要 200-20000 个字符（当前 {len(system_prompt)}）")
        if greeting_message and len(greeting_message) > 200:
            errors.append("greeting_message 不能超过 200 个字符")
        if errors:
            return _field_error(errors)

        avatar_image_key = str(arguments.get("avatar_image_key") or "").strip() or None
        if avatar_image_key and not avatar_image_key.startswith(
            f"images/avatars/characters/{context.user.id}/"
        ):
            return _field_error(["avatar_image_key 不属于当前用户，请使用 generate_avatar 返回的值。"])

        character_in = CharacterCreate(
            name=name,
            description=description,
            system_prompt=system_prompt,
            greeting_message=greeting_message,
            avatar_image_key=avatar_image_key,
            voice_provider=voice.provider,
            voice_model=voice.model or settings.DASHSCOPE_TTS_MODEL,
            voice_provider_voice_id=voice.base_voice_id,
            voice_source_type=voice.source_type,
            voice_profile=voice.profile,
        )
        character = await self._character_service.create_character(
            character_in,
            context.user,
        )
        chat_response = await self._chat_service.create_chat(
            context.user, uuid.UUID(character.id), None
        )

        profile = character.voice_profile or {}
        voice_bundle = {
            **_voice_summary(voice.base_voice_id),
            "speech_rate": profile.get("speech_rate"),
            "volume": profile.get("volume"),
            "pitch_rate": profile.get("pitch_rate"),
        }
        created_payload = {
            "type": "character_created",
            "character_id": character.id,
            "chat_id": str(chat_response.chat.id),
            "name": character.name,
            "description": character.description,
            "avatar_image_key": character.avatar_image_key,
            "visibility": character.visibility,
            "voice": voice_bundle,
        }
        return ToolOutcome(
            llm_result={
                "ok": True,
                "character_id": character.id,
                "chat_id": str(chat_response.chat.id),
                "name": character.name,
                "note": (
                    "角色已直接创建，角色面板已经展示给用户，不需要再点确认。"
                    "现在停下，问用户：有没有需要改进的地方？要不要开始做头像？"
                    "头像有没有参考图（让用户直接发图）、有什么要求？"
                    "用户同意做头像之前不要调用 generate_avatar。"
                ),
            },
            ui_payload=created_payload,
            character_created=created_payload,
        )

    async def _update_character(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        missing = _require_skill(context, "prompt-creation")
        if missing is not None:
            return missing
        target = arguments.get("character_id") or context.session.active_character_id
        if not target:
            return _field_error(["当前会话还没有角色，无法修改。请先调用 create_character。"])
        if context.session.active_character_id and str(target) != str(
            context.session.active_character_id
        ):
            return _field_error(["只能修改当前会话创建的角色。"])

        update_fields: dict[str, Any] = {}
        avatar_image_key = str(arguments.get("avatar_image_key") or "").strip() or None
        if avatar_image_key and not avatar_image_key.startswith(
            f"images/avatars/characters/{context.user.id}/"
        ):
            return _field_error(["avatar_image_key 不属于当前用户，请使用 generate_avatar 返回的值。"])
        if avatar_image_key:
            update_fields_avatar = {"avatar_image_key": avatar_image_key}
        else:
            update_fields_avatar = {}

        for key in (
            "name",
            "description",
            "greeting_message",
            "system_prompt",
        ):
            if key in arguments and arguments[key] is not None:
                update_fields[key] = arguments[key]

        base_profile: dict[str, Any] | None = None
        if str(arguments.get("voice_base_id") or "").strip():
            # A voice swap that names no shaping value keeps what the
            # character already sounds like.
            current = await self._character_service.get_character(
                uuid.UUID(str(target)), current_user=context.user
            )
            base_profile = current.voice_profile or {}

        voice, voice_error = await self._resolve_voice_for_character(
            arguments, context, required=False, base_profile=base_profile
        )
        if voice_error is not None:
            return voice_error
        if voice is not None:
            update_fields.update(
                {
                    "voice_provider": voice.provider,
                    "voice_model": voice.model or settings.DASHSCOPE_TTS_MODEL,
                    "voice_provider_voice_id": voice.base_voice_id,
                    "voice_source_type": voice.source_type,
                    "voice_profile": voice.profile,
                }
            )

        update_fields.update(update_fields_avatar)
        if not update_fields:
            return _field_error(["没有需要修改的字段。"])

        character_update = CharacterUpdate(**update_fields)
        updated = await self._character_service.update_character(
            uuid.UUID(str(target)),
            character_in=character_update,
            current_user=context.user,
        )
        voice = _voice_summary(updated.voice_provider_voice_id)
        ui_payload = {
            "type": "character_updated",
            "character_id": updated.id,
            "chat_id": None,
            "name": updated.name,
            "description": updated.description,
            "avatar_image_key": updated.avatar_image_key,
            "voice": {
                **voice,
                "speech_rate": (updated.voice_profile or {}).get("speech_rate"),
                "volume": (updated.voice_profile or {}).get("volume"),
                "pitch_rate": (updated.voice_profile or {}).get("pitch_rate"),
            },
        }
        return ToolOutcome(
            llm_result={
                "ok": True,
                "character_id": updated.id,
                "name": updated.name,
                "note": "角色已更新。",
            },
            ui_payload=ui_payload,
        )

    async def _search_web(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _field_error(["query 不能为空"])
        num_results = int(arguments.get("num_results") or 5)
        try:
            results = await self._search.search(query=query, num_results=num_results)
        except APIError as exc:
            logger.warning("DeepSeek search failed: %s", exc)
            return ToolOutcome(
                llm_result=_upstream_failure_result(
                    "搜索服务暂时不可用，请基于已有信息继续。",
                    exc,
                    fallback=_SEARCH_FAILURE_FALLBACK,
                )
            )
        except Exception as exc:
            logger.warning("DeepSeek search failed: %s", exc)
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": ["搜索服务暂时不可用，请基于已有信息继续。"],
                    "model_content": _tool_error_model_content(
                        exc, guidance=_SEARCH_FAILURE_FALLBACK
                    ),
                }
            )
        return ToolOutcome(
            llm_result={"ok": True, "query": query, "results": results},
            ui_payload=None,
        )

    async def _read_webpage(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        url = str(arguments.get("url") or "").strip()
        try:
            page = await self._search.fetch_page(url=url)
        except APIError as exc:
            logger.warning("DeepSeek fetch failed: %s", exc)
            return ToolOutcome(
                llm_result=_upstream_failure_result(
                    "网页抓取失败，请换一个来源或基于已有信息继续。",
                    exc,
                    fallback=_FETCH_FAILURE_FALLBACK,
                )
            )
        except Exception as exc:
            logger.warning("DeepSeek fetch failed: %s", exc)
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": ["网页抓取失败，请换一个来源或基于已有信息继续。"],
                    "model_content": _tool_error_model_content(
                        exc, guidance=_FETCH_FAILURE_FALLBACK
                    ),
                }
            )
        return ToolOutcome(llm_result={"ok": True, **page})
