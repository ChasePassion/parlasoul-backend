"""System voice catalogs for TTS providers."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

DEFAULT_MINIMAX_TTS_MODEL = "speech-2.8-turbo"
DEFAULT_MINIMAX_SYSTEM_VOICE_ID = "Chinese (Mandarin)_Wise_Women"
DEFAULT_DASHSCOPE_TTS_MODEL = "qwen3-tts-flash-realtime"
DEFAULT_DASHSCOPE_SYSTEM_VOICE_ID = "Cherry"
# System voices are synthesized by Bailian (DashScope).  The MiniMax catalog
# remains available for legacy bindings and maps 1:1 to the DashScope voices.
DEFAULT_TTS_SYSTEM_PROVIDER = "dashscope"


@dataclass(frozen=True)
class SystemVoiceCatalogItem:
    provider: str
    provider_model: str
    provider_voice_id: str
    display_name: str
    usage_hint: str | None = None
    language: str | None = None
    gender: str | None = None
    avatar_image_key: str | None = None
    preview_audio_filename: str | None = None

    @property
    def preview_audio_url(self) -> str | None:
        if not self.preview_audio_filename:
            return None
        encoded = quote(self.preview_audio_filename, safe="")
        return f"/media/voices/preview/{self.provider}/{encoded}"

    def to_dict(self, *, provider_model: str | None = None) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "provider_model": provider_model or self.provider_model,
            "provider_voice_id": self.provider_voice_id,
            "display_name": self.display_name,
            "usage_hint": self.usage_hint,
            "preview_audio_url": self.preview_audio_url,
            "language": self.language,
            "gender": self.gender,
            "avatar_image_key": self.avatar_image_key,
        }


_DASHSCOPE_LEGACY_VOICES: tuple[dict[str, str | None], ...] = (
    {
        "provider_voice_id": "Cherry",
        "display_name": "芊悦",
        "usage_hint": "温暖阳光少女",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/df6b8b7237b24396aebadd08a627e938",
    },
    {
        "provider_voice_id": "Serena",
        "display_name": "苏瑶",
        "usage_hint": "温柔小姐姐",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/483fb9cf745e41e9bb1c4f63fc0493a6",
    },
    {
        "provider_voice_id": "Ethan",
        "display_name": "晨煦",
        "usage_hint": "活力阳光男",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/e95590f5a4314e098307c2407b078497",
    },
    {
        "provider_voice_id": "Chelsie",
        "display_name": "千雪",
        "usage_hint": "二次元虚拟女友",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/856f560754f94f58b2f7af051fea68d7",
    },
    {
        "provider_voice_id": "Momo",
        "display_name": "茉兔",
        "usage_hint": "撒娇搞怪少女",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/a43f593a41c4470e95808b3e043c31a2",
    },
    {
        "provider_voice_id": "Vivian",
        "display_name": "十三",
        "usage_hint": "拽萌小暴脾气",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/c6e424255a0b47afbdae7245deb5e5fb",
    },
    {
        "provider_voice_id": "Moon",
        "display_name": "月白",
        "usage_hint": "率性帅气",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/f8f6d860eb1947548b5fe930d67e79f5",
    },
    {
        "provider_voice_id": "Maia",
        "display_name": "四月",
        "usage_hint": "知性与温柔的碰撞",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/6842d945a890472b9a2d2883125dc9ae",
    },
    {
        "provider_voice_id": "Kai",
        "display_name": "凯",
        "usage_hint": "耳朵的一场 SPA",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/c1ca0129ea00430caf272882176e1b20",
    },
    {
        "provider_voice_id": "Nofish",
        "display_name": "不吃鱼",
        "usage_hint": "翘舌困难设计师",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/36f626b3d6c344bf9d6cfe37c4253458",
    },
    {
        "provider_voice_id": "Bella",
        "display_name": "萌宝",
        "usage_hint": "可爱小萝莉",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/68687adc781b4bc7ba5c9533f5a83159",
    },
    {
        "provider_voice_id": "Eldric Sage",
        "display_name": "沧明子",
        "usage_hint": "沉稳睿智老者",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/2e185ff240594e9ca41359fe4d630505",
    },
    {
        "provider_voice_id": "Mia",
        "display_name": "乖小妹",
        "usage_hint": "温顺乖巧少女",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/3867b24c2396439396a62a7ef6a41a07",
    },
    {
        "provider_voice_id": "Mochi",
        "display_name": "沙小弥",
        "usage_hint": "伶俐小大人",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/310741c03faa496784b2db725a9d5435",
    },
    {
        "provider_voice_id": "Bellona",
        "display_name": "燕铮莺",
        "usage_hint": "热血江湖女侠",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/3877e6e569cf4ee6a7b9a0ac207644f0",
    },
    {
        "provider_voice_id": "Vincent",
        "display_name": "田叔",
        "usage_hint": "沙哑烟嗓大叔",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/ee98fd0dde394f13ba57d18c2bb6d028",
    },
    {
        "provider_voice_id": "Bunny",
        "display_name": "萌小姬",
        "usage_hint": "萌系小萝莉",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/e9ecb32af27b456fae323ec154337bd4",
    },
    {
        "provider_voice_id": "Neil",
        "display_name": "阿闻",
        "usage_hint": "字正腔圆",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/abb934dd6a9947b689018c483c555d63",
    },
    {
        "provider_voice_id": "Elias",
        "display_name": "墨讲师",
        "usage_hint": "严谨知识讲解",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/5d9393a243c14ea2a97f4ef1305b85f1",
    },
    {
        "provider_voice_id": "Arthur",
        "display_name": "徐大爷",
        "usage_hint": "质朴故事感",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/c32211d5306444449c28d5cf782c9738",
    },
    {
        "provider_voice_id": "Nini",
        "display_name": "邻家妹妹",
        "usage_hint": "软糯甜黏",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/be61a021b6fe48829b1f0cab3221505b",
    },
    {
        "provider_voice_id": "Seren",
        "display_name": "小婉",
        "usage_hint": "舒缓助眠",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/355047e814674d86be1ad617e8d95c0e",
    },
    {
        "provider_voice_id": "Pip",
        "display_name": "顽屁小孩",
        "usage_hint": "调皮捣蛋、童真感",
        "language": "中英文",
        "gender": "男",
        "avatar_image_key": "images/avatars/voices/system/c8adf30c359f47f8a7b486d220c04f9b",
    },
    {
        "provider_voice_id": "Stella",
        "display_name": "少女阿月",
        "usage_hint": "甜美正义少女",
        "language": "中英文",
        "gender": "女",
        "avatar_image_key": "images/avatars/voices/system/30dd1b07fdd647f8b5de022bcbc02de7",
    },
)

DASHSCOPE_REMOVED_SYSTEM_VOICE_IDS = frozenset({"Mochi", "Bellona", "Neil", "Arthur", "Pip"})

_MINIMAX_VOICE_ID_BY_DASHSCOPE_ID = {
    "Cherry": "Chinese (Mandarin)_Wise_Women",
    "Serena": "Chinese (Mandarin)_Gentle_Senior",
    "Ethan": "badao_shaoye",
    "Chelsie": "female-yujie",
    "Momo": "Arrogant_Miss",
    "Vivian": "Attractive_Girl",
    "Moon": "Chinese (Mandarin)_Gentleman",
    "Maia": "female-chengshu",
    "Kai": "bingjiao_didi",
    "Nofish": "Chinese (Mandarin)_Gentle_Youth",
    "Bella": "Chinese_Mandarin_Warm_Girl",
    "Eldric Sage": "Santa_Claus",
    "Mia": "female-shaonv",
    "Vincent": "Arnold",
    "Bunny": "Chinese (Mandarin)_Cute_Spirit",
    "Elias": "Sweet_Girl",
    "Nini": "Chinese (Mandarin)_Sweet_Lady",
    "Seren": "Chinese (Mandarin)_Mature_Woman",
    "Stella": "Chinese (Mandarin)_Soft_Girl",
}

_DASHSCOPE_VOICE_ID_BY_MINIMAX_ID = {
    minimax_voice_id: dashscope_voice_id
    for dashscope_voice_id, minimax_voice_id in _MINIMAX_VOICE_ID_BY_DASHSCOPE_ID.items()
}

_MINIMAX_PREVIEW_FILE_BY_VOICE_ID = {
    "Chinese (Mandarin)_Wise_Women": "Chinese_Mandarin_Wise_Women.mp3",
    "Chinese (Mandarin)_Gentle_Senior": "Chinese_Mandarin_Gentle_Senior.mp3",
    "badao_shaoye": "badao_shaoye.mp3",
    "female-yujie": "female-yujie.mp3",
    "Arrogant_Miss": "Arrogant_Miss.mp3",
    "Attractive_Girl": "Attractive_Girl.mp3",
    "Chinese (Mandarin)_Gentleman": "Chinese_Mandarin_Gentleman.mp3",
    "female-chengshu": "female-chengshu.mp3",
    "bingjiao_didi": "bingjiao_didi.mp3",
    "Chinese (Mandarin)_Gentle_Youth": "Chinese_Mandarin_Gentle_Youth.mp3",
    "Chinese_Mandarin_Warm_Girl": "Chinese_Mandarin_Warm_Girl.mp3",
    "Santa_Claus": "Santa_Claus.mp3",
    "female-shaonv": "female-shaonv.mp3",
    "Arnold": "Arnold.mp3",
    "Chinese (Mandarin)_Cute_Spirit": "Chinese_Mandarin_Cute_Spirit.mp3",
    "Sweet_Girl": "Sweet_Girl.mp3",
    "Chinese (Mandarin)_Sweet_Lady": "Chinese_Mandarin_Sweet_Lady.mp3",
    "Chinese (Mandarin)_Mature_Woman": "Chinese_Mandarin_Mature_Woman.mp3",
    "Chinese (Mandarin)_Soft_Girl": "Chinese_Mandarin_Soft_Girl.mp3",
}


def dashscope_legacy_voice_catalog(
    *, provider_model: str = DEFAULT_DASHSCOPE_TTS_MODEL
) -> tuple[SystemVoiceCatalogItem, ...]:
    return tuple(
        SystemVoiceCatalogItem(
            provider="dashscope",
            provider_model=provider_model,
            provider_voice_id=str(item["provider_voice_id"]),
            display_name=str(item["display_name"]),
            usage_hint=item.get("usage_hint"),
            language=item.get("language"),
            gender=item.get("gender"),
            avatar_image_key=item.get("avatar_image_key"),
            preview_audio_filename=f"{item['provider_voice_id']}.mp3",
        )
        for item in _DASHSCOPE_LEGACY_VOICES
        if str(item["provider_voice_id"]) not in DASHSCOPE_REMOVED_SYSTEM_VOICE_IDS
    )


def minimax_system_voice_catalog(
    *, provider_model: str = DEFAULT_MINIMAX_TTS_MODEL
) -> tuple[SystemVoiceCatalogItem, ...]:
    items: list[SystemVoiceCatalogItem] = []
    legacy_by_id = {str(item["provider_voice_id"]): item for item in _DASHSCOPE_LEGACY_VOICES}
    for dashscope_voice_id, minimax_voice_id in _MINIMAX_VOICE_ID_BY_DASHSCOPE_ID.items():
        legacy = legacy_by_id[dashscope_voice_id]
        items.append(
            SystemVoiceCatalogItem(
                provider="minimax",
                provider_model=provider_model,
                provider_voice_id=minimax_voice_id,
                display_name=str(legacy["display_name"]),
                usage_hint=legacy.get("usage_hint"),
                language=legacy.get("language"),
                gender=legacy.get("gender"),
                avatar_image_key=legacy.get("avatar_image_key"),
                preview_audio_filename=_MINIMAX_PREVIEW_FILE_BY_VOICE_ID[minimax_voice_id],
            )
        )
    return tuple(items)


def minimax_voice_id_for_legacy_dashscope(voice_id: str) -> str:
    return _MINIMAX_VOICE_ID_BY_DASHSCOPE_ID.get(
        voice_id,
        DEFAULT_MINIMAX_SYSTEM_VOICE_ID,
    )


def legacy_dashscope_voice_id_for_minimax(voice_id: str) -> str | None:
    return _DASHSCOPE_VOICE_ID_BY_MINIMAX_ID.get(voice_id)


__all__ = [
    "DASHSCOPE_REMOVED_SYSTEM_VOICE_IDS",
    "DEFAULT_DASHSCOPE_SYSTEM_VOICE_ID",
    "DEFAULT_DASHSCOPE_TTS_MODEL",
    "DEFAULT_MINIMAX_SYSTEM_VOICE_ID",
    "DEFAULT_MINIMAX_TTS_MODEL",
    "DEFAULT_TTS_SYSTEM_PROVIDER",
    "SystemVoiceCatalogItem",
    "dashscope_legacy_voice_catalog",
    "legacy_dashscope_voice_id_for_minimax",
    "minimax_system_voice_catalog",
    "minimax_voice_id_for_legacy_dashscope",
]
