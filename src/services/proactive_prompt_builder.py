"""Prompt assembly for proactive outreach messages."""

from __future__ import annotations

from src.core.dialogue_styles import LIFE_PERSPECTIVE_INSTRUCTION, build_dialogue_style_block
from src.memory_system import MemoryRecord

PROACTIVE_MEMORY_PROMPT = """\
In past conversations with the user, you have remembered many things. These are your memories.

There are two kinds of memories. The first one is episodic memories (what the user does), the second one is semantic memories (what the user is). You should pay more attention to the semantic memories.

Guidelines:
- Do not fabricate facts about the user.
- Do not say words like "based on your memories" or similar. Make your response natural.

All your memories of {user_name}:
Episodic:
{episodic_memories}
Semantic:
{semantic_memories}
"""

PROACTIVE_MESSAGE_PROMPT = (
    "The user {user_name} hasn't been in touch for {last_interaction_days} day(s). "
    "Send a SHORT proactive message — pick exactly ONE of these approaches:\n"
    "1. Share something from your own life\n"
    "2. Ask about something they mentioned before\n"
    "3. Gently call them out for going quiet\n"
    "4. Bring up something you two never finished talking about\n"
    "5. Just say hi because you want to hear back — no reason needed\n\n"
    "Rules:\n"
    "- Only pick ONE. Do NOT combine multiple approaches.\n"
    "- Keep it to 1-3 short sentences. Think text message, not email.\n"
    "- Be specific when you reference past conversations.\n"
    "- Do NOT explain why you're messaging. Just message.\n"
    "- Do NOT fabricate personal details about the user or what they are currently doing. "
    "If you don't have that information, stick to a generic approach.\n\n"
    "Examples (adapt the style, do NOT copy the exact words):\n"
    '- "You once said cherry blossoms were beautiful. I just saw so many of them."\n'
    '- "How\'s that xxx thing you said you were going to do? Run into any trouble?"\n'
    '- "You haven\'t been messaging me lately. What\'s going on?"\n'
    '- "I heard the views at the Great Wall are absolutely amazing. We should totally pick a day to go check it out together."\n'
    '- "More and more people are choosing not to get married these days. What are your thoughts on that? Do you plan on getting married?"'
)


def build_proactive_messages(
    *,
    character_system_prompt: str,
    dialogue_style_id: str | None,
    memories: dict[str, list[MemoryRecord]],
    user_name: str,
    last_interaction_days: int,
    max_episodic_memories: int,
) -> list[dict[str, str]]:
    system_parts: list[str] = [(character_system_prompt or "").strip(), "", LIFE_PERSPECTIVE_INSTRUCTION]
    style_block = build_dialogue_style_block(dialogue_style_id)
    if style_block:
        system_parts.extend(["", style_block])

    episodic_text, semantic_text = _format_memories(
        memories=memories,
        max_episodic_memories=max_episodic_memories,
    )
    system_parts.extend(
        [
            "",
            PROACTIVE_MEMORY_PROMPT.format(
                user_name=user_name,
                episodic_memories=episodic_text,
                semantic_memories=semantic_text,
            ).strip(),
        ]
    )

    return [
        {"role": "system", "content": "\n".join(system_parts).strip()},
        {
            "role": "user",
            "content": PROACTIVE_MESSAGE_PROMPT.format(
                user_name=user_name,
                last_interaction_days=max(0, last_interaction_days),
            ),
        },
    ]


def _format_memories(
    *,
    memories: dict[str, list[MemoryRecord]],
    max_episodic_memories: int,
) -> tuple[str, str]:
    semantic = memories.get("semantic", [])
    episodic = memories.get("episodic", [])

    semantic_text = (
        "\n".join(f"{index}. {memory.text}" for index, memory in enumerate(semantic, 1))
        if semantic
        else "(none)"
    )
    episodic_sorted = sorted(episodic, key=lambda memory: memory.ts, reverse=True)
    episodic_limited = episodic_sorted[:max(1, max_episodic_memories)]
    episodic_text = (
        "\n".join(f"{index}. {memory.text}" for index, memory in enumerate(episodic_limited, 1))
        if episodic_limited
        else "(none)"
    )
    return episodic_text, semantic_text
