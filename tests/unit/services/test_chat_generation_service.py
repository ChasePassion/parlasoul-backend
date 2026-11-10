"""Prompt layout tests for the shared generation seam.

These pin the section ordering of the system prompt that chat, turn and
realtime production paths (and the evaluation replay runner) share. Marker
values are used for dynamic blocks so rewording production copy does not
break the tests; reordering or dropping a section does.
"""

from __future__ import annotations

from typing import Any

from src.memory_system import MemoryRecord
from src.memory_system.prompts import MEMORY_ANSWER_PROMPT
from src.services.chat_generation_service import (
    VOICE_CHANNEL_POLICY,
    ChatGenerationContext,
    build_llm_system_prompt,
)

_CHARACTER_PROMPT = "You are Alex, a calm English-learning companion."


def _record(text: str, memory_type: str, index: int) -> MemoryRecord:
    return MemoryRecord(
        id=index,
        user_id="user-1",
        character_id="character-1",
        memory_type=memory_type,
        ts=1_700_000_000 + index,
        chat_id="chat-1",
        text=text,
    )


def _context(**overrides: Any) -> ChatGenerationContext:
    base: dict[str, Any] = {
        "character_system_prompt": _CHARACTER_PROMPT,
        "history_messages": [],
        "user_message": "hi",
        "memories": {"episodic": [], "semantic": []},
        "use_all_semantic": False,
        "preferred_expressions": [],
    }
    base.update(overrides)
    return ChatGenerationContext(**base)


def test_prompt_layout_preserves_section_ordering() -> None:
    context = _context(
        character_system_prompt="<<CHAR_PROMPT>>",
        memories={
            "episodic": [_record("<<EPISODIC_1>>", "episodic", 1)],
            "semantic": [_record("<<SEMANTIC_1>>", "semantic", 2)],
        },
        preferred_expressions=["<<PREFERRED>>"],
        memory_active=True,
        channel_policy="voice",
    )

    prompt = build_llm_system_prompt(context)

    assert prompt.startswith("<<CHAR_PROMPT>>")
    assert prompt.endswith(VOICE_CHANNEL_POLICY.strip())
    assert (
        prompt.index("<<EPISODIC_1>>")
        < prompt.index("<<SEMANTIC_1>>")
        < prompt.index("<<PREFERRED>>")
        < prompt.index(VOICE_CHANNEL_POLICY.strip())
    )


def test_memory_disabled_omits_memory_block_entirely() -> None:
    context = _context(
        memories={
            "episodic": [_record("episode", "episodic", 1)],
            "semantic": [_record("fact", "semantic", 2)],
        },
        memory_active=False,
    )

    prompt = build_llm_system_prompt(context)

    assert MEMORY_ANSWER_PROMPT.strip() not in prompt
    assert "Retrieved memories" not in prompt
    assert "(none)" not in prompt
    assert "episode" not in prompt




def test_episodic_truncated_to_five_and_semantic_sliced() -> None:
    context = _context(
        memories={
            "episodic": [_record(f"episode {i}", "episodic", i) for i in range(1, 8)],
            "semantic": [_record(f"fact {i}", "semantic", i) for i in range(1, 8)],
        },
        use_all_semantic=False,
    )

    prompt = build_llm_system_prompt(context)

    assert "5. episode 5" in prompt
    assert "episode 6" not in prompt
    assert "5. fact 5" in prompt
    assert "fact 6" not in prompt




