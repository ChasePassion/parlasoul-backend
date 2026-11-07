"""System voice catalog for the tuning console.

Source: 百炼 (Bailian / DashScope) 音色列表
https://help.aliyun.com/zh/model-studio/qwen-tts-voice-list

这 48 个音色适用于 ``qwen3-tts-flash-realtime`` / ``qwen3-tts-flash``。
``qwen3-tts-vc-realtime`` 只接受声音复刻生成的音色 ID，因此克隆音色从数据库读取，
不在这张表里。

这张表只服务于本地调音台，不参与线上音色目录；线上目录仍由
``src/core/tts_voice_catalog.py`` 与 ``VoiceGatewayRepository`` 维护。
"""

from __future__ import annotations

from dataclasses import dataclass

# (provider_voice_id, 中文名, 风格描述, 语言分组)
_VOICES: tuple[tuple[str, str, str, str], ...] = (
    ("Cherry", "芊悦", "阳光积极、亲切自然女声", "普通话"),
    ("Serena", "苏瑶", "温柔女声", "普通话"),
    ("Ethan", "晨煦", "阳光温暖、有朝气，带北方口音男声", "普通话"),
    ("Chelsie", "千雪", "二次元虚拟女友女声", "普通话"),
    ("Momo", "茉兔", "撒娇搞怪女声", "普通话"),
    ("Vivian", "十三", "拽萌、小暴躁女声", "普通话"),
    ("Moon", "月白", "率性帅气男声", "普通话"),
    ("Maia", "四月", "知性温柔女声", "普通话"),
    ("Kai", "凯", "放松听感男声", "普通话"),
    ("Nofish", "不吃鱼", "带口音的设计师男声", "普通话"),
    ("Bella", "萌宝", "小萝莉女声", "普通话"),
    ("Jennifer", "詹妮弗", "有电影质感的美语女声", "普通话"),
    ("Ryan", "甜茶", "节奏强、戏感足男声", "普通话"),
    ("Katerina", "卡捷琳娜", "御姐音、韵律感女声", "普通话"),
    ("Aiden", "艾登", "精通厨艺的美语大男孩", "普通话"),
    ("Eldric Sage", "沧明子", "沉稳睿智老者男声", "普通话"),
    ("Mia", "乖小妹", "温顺乖巧女声", "普通话"),
    ("Mochi", "沙小弥", "早慧小大人男声", "普通话"),
    ("Bellona", "燕铮莺", "洪亮清晰、有江湖感女声", "普通话"),
    ("Vincent", "田叔", "沙哑烟嗓、江湖豪情男声", "普通话"),
    ("Bunny", "萌小姬", "萌系小萝莉女声", "普通话"),
    ("Neil", "阿闻", "新闻主持人男声", "普通话"),
    ("Elias", "墨讲师", "严谨又善叙事的讲师女声", "普通话"),
    ("Arthur", "徐大爷", "质朴沧桑、讲故事男声", "普通话"),
    ("Nini", "邻家妹妹", "软黏甜美妹妹女声", "普通话"),
    ("Seren", "小婉", "温和舒缓助眠女声", "普通话"),
    ("Pip", "顽屁小孩", "调皮童真男童声", "普通话"),
    ("Stella", "少女阿月", "甜美迷糊少女，喊口号时有正义感", "普通话"),
    ("Bodega", "博德加", "热情西班牙大叔", "普通话"),
    ("Sonrisa", "索尼莎", "热情开朗拉美大姐", "普通话"),
    ("Alek", "阿列克", "冷中有暖的俄罗斯男声", "普通话"),
    ("Dolce", "多尔切", "慵懒意大利大叔", "普通话"),
    ("Sohee", "素熙", "温柔开朗韩国女声", "普通话"),
    ("Ono Anna", "小野杏", "鬼灵精怪青梅竹马女声", "普通话"),
    ("Lenn", "莱恩", "理性叛逆德国青年男声", "普通话"),
    ("Emilien", "埃米尔安", "浪漫法国大哥哥", "普通话"),
    ("Andre", "安德雷", "磁性沉稳男声", "普通话"),
    ("Radio Gol", "拉迪奥·戈尔", "足球解说男声", "普通话"),
    ("Jada", "上海-阿珍", "风火沪上阿姐女声", "方言：上海话"),
    ("Dylan", "北京-晓东", "胡同少年男声", "方言：北京话"),
    ("Li", "南京-老李", "耐心瑜伽老师男声", "方言：南京话"),
    ("Marcus", "陕西-秦川", "老陕味道男声", "方言：陕西话"),
    ("Roy", "闽南-阿杰", "诙谐直爽台湾哥仔男声", "方言：闽南语"),
    ("Peter", "天津-李彼得", "天津相声捧哏男声", "方言：天津话"),
    ("Sunny", "四川-晴儿", "甜美川妹子女声", "方言：四川话"),
    ("Eric", "四川-程川", "跳脱市井成都男子", "方言：四川话"),
    ("Rocky", "粤语-阿强", "幽默陪聊男声", "方言：粤语"),
    ("Kiki", "粤语-阿清", "甜美港妹闺蜜女声", "方言：粤语"),
)


@dataclass(frozen=True)
class CatalogVoice:
    provider_voice_id: str
    display_name: str
    hint: str
    language: str


VOICE_CATALOG: tuple[CatalogVoice, ...] = tuple(
    CatalogVoice(voice_id, name, hint, language) for voice_id, name, hint, language in _VOICES
)

# 项目自有的 19 个音色里，有几个不在上面的官方列表里（历史绑定）。调音台把它们一并展示，
# 避免"角色正在用的音色在列表里找不到"。
VOICE_LANGUAGES: tuple[str, ...] = tuple(
    dict.fromkeys(item.language for item in VOICE_CATALOG)
)

__all__ = ["CatalogVoice", "VOICE_CATALOG", "VOICE_LANGUAGES"]
