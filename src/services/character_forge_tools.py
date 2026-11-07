"""Tool schemas, preview tokens and execution for the character-forge agent."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from src.core.config import settings
from src.core.exceptions import APIError
from src.core.tts_registry import DEFAULT_SYSTEM_TTS_PROVIDER
from src.core.tts_voice_catalog import dashscope_legacy_voice_catalog
from src.core.voice_profiles import normalize_voice_profile
from src.db.models import (
    CharacterForgeMessage,
    CharacterForgeSession,
    User,
)
from src.repositories.exa_repository import ExaRepository
from src.schemas.character import CharacterCreate, CharacterUpdate
from src.schemas.media import MediaUploadKind
from src.services.avatar_generation_service import AvatarGenerationService
from src.services.character_service import CharacterService
from src.services.chat_service import ChatService
from src.services.forge_skills import SKILL_NAMES, load_forge_skill
from src.services.media_url_service import (
    build_r2_public_url,
    original_object_key,
    validate_avatar_image_key_owner,
)

logger = logging.getLogger(__name__)

_PREVIEW_TTL_SECONDS = 60 * 60 * 6
_MAX_SAMPLE_CHARS = 200
_MIN_SAMPLE_CHARS = 10
_MAX_REFERENCE_IMAGES = 3

VOICE_CATALOG = {item.provider_voice_id: item for item in dashscope_legacy_voice_catalog()}


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


def build_tool_schemas(
    *, reference_image_keys: list[str] | None = None
) -> list[dict[str, Any]]:
    voice_id_schema = {"type": "string", "enum": _voice_ids()}
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
                    "返回 preview_id 和可播放的试听地址。创建角色时必须使用这里返回的 preview_id。"
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
                        "voice_preview_id": {
                            "type": "string",
                            "description": "preview_voice 返回的 preview_id",
                        },
                        "avatar_image_key": {
                            "type": "string",
                            "description": "generate_avatar 返回的 avatar_image_key",
                        },
                    },
                    "required": [
                        "name",
                        "description",
                        "system_prompt",
                        "voice_preview_id",
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
                    "换声音时传新的 voice_preview_id；角色 id 默认取当前会话的角色。"
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
                        "voice_preview_id": {"type": "string"},
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
                "description": "用 Exa 做语义网络搜索，返回标题、URL 和摘要片段。",
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
    search_calls: list[int] = field(default_factory=list)
    avatar_calls: list[int] = field(default_factory=list)
    loaded_skills: set[str] = field(default_factory=set)
    reference_images: dict[str, str] = field(default_factory=dict)


def _require_skill(context: ToolContext, name: str) -> ToolOutcome | None:
    if name in context.loaded_skills:
        return None
    return _field_error(
        [f"请先调用 load_skill 加载 {name} 技能，再执行这个操作。"]
    )


def _sign_preview_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{body}.{signature}"


def decode_preview_token(token: str, *, user_id: uuid.UUID) -> dict[str, Any]:
    try:
        body, signature = token.split(".", 1)
    except ValueError as exc:
        raise APIError(
            status_code=422,
            code="validation_failed",
            message="validation failed: invalid voice preview id",
        ) from exc

    expected = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        raise APIError(
            status_code=422,
            code="validation_failed",
            message="validation failed: invalid voice preview id",
        )

    padding = "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + padding))
    except Exception as exc:
        raise APIError(
            status_code=422,
            code="validation_failed",
            message="validation failed: invalid voice preview id",
        ) from exc

    if payload.get("uid") != str(user_id):
        raise APIError(
            status_code=403,
            code="forbidden",
            message="authorization failed: voice preview belongs to another user",
        )
    if int(payload.get("exp", 0)) < int(time.time()):
        raise APIError(
            status_code=422,
            code="validation_failed",
            message="validation failed: voice preview expired",
        )
    return payload


def _voice_summary(voice_id: str) -> dict[str, Any]:
    item = VOICE_CATALOG[voice_id]
    return {
        "provider_voice_id": item.provider_voice_id,
        "display_name": item.display_name,
        "usage_hint": item.usage_hint,
        "gender": item.gender,
    }


def _field_error(errors: list[str]) -> ToolOutcome:
    return ToolOutcome(llm_result={"ok": False, "errors": errors})


class ForgeToolExecutor:
    def __init__(
        self,
        *,
        character_service: CharacterService,
        chat_service: ChatService,
        exa_repository: ExaRepository,
        avatar_service: AvatarGenerationService,
    ):
        self._character_service = character_service
        self._chat_service = chat_service
        self._exa = exa_repository
        self._avatars = avatar_service

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
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return _field_error([f"未知工具：{tool_name}"])
        try:
            return await handler(arguments, context)
        except APIError as exc:
            return ToolOutcome(llm_result={"ok": False, "errors": [exc.message]})
        except ValidationError as exc:
            messages = [
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            ]
            return ToolOutcome(llm_result={"ok": False, "errors": messages})

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
        payload = {
            "uid": str(context.user.id),
            "exp": int(time.time()) + _PREVIEW_TTL_SECONDS,
            "base_voice_id": base_voice_id,
            "voice_profile": profile.to_dict(),
            "sample_text": sample_text,
        }
        preview_id = _sign_preview_payload(payload)
        voice = _voice_summary(base_voice_id)
        preview_url = f"/v1/character-forge/voice-previews/{preview_id}/audio"
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
                "note": "试听已生成并展示给用户。继续调用 create_character 绑定这个 preview_id。",
            },
            ui_payload=ui_payload,
        )

    async def _generate_avatar(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        missing = _require_skill(context, "avatar-creation")
        if missing is not None:
            return missing
        if len(context.avatar_calls) >= settings.CHARACTER_FORGE_MAX_AVATAR_CALLS:
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": ["本次对话的形象生成次数已用完。可以不带头像继续创建角色。"],
                }
            )
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

        context.avatar_calls.append(1)
        image_key, _avatar_urls = await self._avatars.generate(
            user_id=context.user.id,
            prompt=prompt,
            reference_images=reference_images or None,
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

        preview_id = str(arguments.get("voice_preview_id") or "").strip()
        if not preview_id:
            return _field_error(["缺少 voice_preview_id。请先调用 preview_voice。"])
        preview = decode_preview_token(preview_id, user_id=context.user.id)

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
            voice_provider=DEFAULT_SYSTEM_TTS_PROVIDER,
            voice_model=settings.DASHSCOPE_TTS_MODEL,
            voice_provider_voice_id=str(preview["base_voice_id"]),
            voice_source_type="system",
            voice_profile=preview.get("voice_profile") or {},
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
            **_voice_summary(str(preview["base_voice_id"])),
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

        preview_id = str(arguments.get("voice_preview_id") or "").strip()
        if preview_id:
            preview = decode_preview_token(preview_id, user_id=context.user.id)
            update_fields.update(
                {
                    "voice_provider": DEFAULT_SYSTEM_TTS_PROVIDER,
                    "voice_model": settings.DASHSCOPE_TTS_MODEL,
                    "voice_provider_voice_id": preview["base_voice_id"],
                    "voice_source_type": "system",
                    "voice_profile": preview.get("voice_profile"),
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
        if len(context.search_calls) >= settings.CHARACTER_FORGE_MAX_SEARCH_CALLS:
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": ["本次对话的搜索次数已用完，请基于已有信息继续。"],
                }
            )
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _field_error(["query 不能为空"])
        context.search_calls.append(1)
        num_results = int(arguments.get("num_results") or 5)
        try:
            results = await self._exa.search(query=query, num_results=num_results)
        except Exception as exc:
            logger.warning("Exa search failed: %s", exc)
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": ["搜索服务暂时不可用，请基于已有信息继续。"],
                }
            )
        return ToolOutcome(
            llm_result={"ok": True, "query": query, "results": results},
            ui_payload=None,
        )

    async def _read_webpage(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> ToolOutcome:
        if len(context.search_calls) >= settings.CHARACTER_FORGE_MAX_SEARCH_CALLS:
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": ["本次对话的搜索次数已用完，请基于已有信息继续。"],
                }
            )
        url = str(arguments.get("url") or "").strip()
        context.search_calls.append(1)
        try:
            page = await self._exa.get_contents(url=url)
        except Exception as exc:
            logger.warning("Exa contents failed: %s", exc)
            return ToolOutcome(
                llm_result={
                    "ok": False,
                    "errors": ["网页抓取失败，请换一个来源或基于已有信息继续。"],
                }
            )
        return ToolOutcome(llm_result={"ok": True, **page})
