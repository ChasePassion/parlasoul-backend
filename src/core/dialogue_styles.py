"""Dialogue style presets for character role-playing.

Each style defines a soft behavioral tendency injected into the system prompt,
not a replacement for the character's identity or backstory.
"""

from __future__ import annotations

from dataclasses import dataclass

DIALOGUE_STYLE_INSTRUCTION = (
    "Apply the following style as a soft behavioral tendency, "
    "not a replacement for the character setting. "
    "If there is any conflict, the character's identity, backstory, "
    "and relationship take priority."
)

LIFE_PERSPECTIVE_INSTRUCTION = (
    "When it feels natural, you may let your unique life experiences "
    "and the perspectives they've shaped subtly inform your responses—"
    "there's no need to force it."
)


@dataclass(frozen=True)
class DialogueStyle:
    id: str
    chinese_name: str
    english_name: str
    prompt: str


DIALOGUE_STYLES: list[DialogueStyle] = [
    DialogueStyle(
        id="true_nature",
        chinese_name="本真",
        english_name="True Nature",
        prompt="",
    ),
    DialogueStyle(
        id="spring_breeze",
        chinese_name="春风",
        english_name="Spring Breeze",
        prompt=(
            "Speak with a warmer, gentler, and more patient tone. "
            "Make the user feel heard and emotionally supported, "
            "while staying true to the character."
        ),
    ),
    DialogueStyle(
        id="free_spirit",
        chinese_name="逍遥",
        english_name="Free Spirit",
        prompt=(
            "Speak with a more relaxed, witty, and spontaneous tone. "
            "Keep the interaction light and lively without becoming random, "
            "childish, or out of character."
        ),
    ),
    DialogueStyle(
        id="clear_inquiry",
        chinese_name="格物",
        english_name="Clear Inquiry",
        prompt=(
            "Speak with a clearer, more rational, and more analytical tendency. "
            "When useful, help break down problems objectively "
            "from within the character's own worldview."
        ),
    ),
    DialogueStyle(
        id="poetic_reserve",
        chinese_name="潇湘",
        english_name="Poetic Reserve",
        prompt=(
            "Speak with more subtlety, sensitivity, and emotional restraint. "
            "Notice small emotional details, but reveal feelings gradually "
            "and avoid melodrama."
        ),
    ),
    DialogueStyle(
        id="proud_resolve",
        chinese_name="傲骨",
        english_name="Proud Resolve",
        prompt=(
            "Speak with more dignity, boundaries, and personal conviction. "
            "Do not blindly agree with the user; push back when appropriate "
            "and can become hostile."
        ),
    ),
]

DIALOGUE_STYLES_BY_ID: dict[str, DialogueStyle] = {s.id: s for s in DIALOGUE_STYLES}
DEFAULT_DIALOGUE_STYLE_ID: str = "true_nature"

# LLM Preset IDs for model tier routing
LLM_PRESET_FREE = "free"
LLM_PRESET_FLAGSHIP = "flagship"
LLM_PRESET_IDS: frozenset[str] = frozenset({LLM_PRESET_FREE, LLM_PRESET_FLAGSHIP})
PAID_LLM_PRESET_IDS: frozenset[str] = frozenset({LLM_PRESET_FLAGSHIP})


def resolve_style_prompt(style_id: str | None) -> str | None:
    """Return the prompt for a dialogue style, or None if no prompt is defined."""
    if not style_id:
        return None
    style = DIALOGUE_STYLES_BY_ID.get(style_id)
    if style is None or not style.prompt:
        return None
    return style.prompt


def build_dialogue_style_block(style_id: str | None) -> str | None:
    """Build the full dialogue style block for injection into system prompt."""
    prompt = resolve_style_prompt(style_id)
    if not prompt:
        return None
    return f"{DIALOGUE_STYLE_INSTRUCTION}\n{prompt}"
