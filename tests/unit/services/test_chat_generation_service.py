"""Prompt layout tests for the shared generation seam.

These pin the section ordering of the system prompt that chat, turn and
realtime production paths (and the evaluation replay runner) share. Marker
values are used for dynamic blocks so rewording production copy does not
break the tests; reordering or dropping a section does.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.memory_system import MemoryRecord
from src.memory_system.prompts import MEMORY_ANSWER_PROMPT
from src.schemas.chat import ChatImageRef
from src.services.chat_generation_service import (
    VOICE_CHANNEL_POLICY,
    ChatGenerationContext,
    build_generation_messages,
    build_llm_system_prompt,
)
from src.services.chat_learning_service import ChatLearningService
from src.services.chat_service import _build_user_message_with_images

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


def test_memory_active_empty_memories_render_none_placeholders() -> None:
    prompt = build_llm_system_prompt(_context(memory_active=True))

    assert "Episodic:\n(none)" in prompt
    assert "Semantic:\n(none)" in prompt


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


def test_use_all_semantic_keeps_every_semantic_memory() -> None:
    context = _context(
        memories={
            "episodic": [],
            "semantic": [_record(f"fact {i}", "semantic", i) for i in range(1, 8)],
        },
        use_all_semantic=True,
    )

    prompt = build_llm_system_prompt(context)

    assert "7. fact 7" in prompt


def test_invalid_channel_policy_rejected() -> None:
    context = _context(channel_policy="video")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="invalid channel_policy"):
        build_llm_system_prompt(context)
    with pytest.raises(ValueError, match="invalid channel_policy"):
        build_generation_messages(context, user_message_builder=_build_user_message_with_images)


def test_chat_channel_does_not_append_voice_policy() -> None:
    prompt = build_llm_system_prompt(_context(channel_policy="chat"))

    assert VOICE_CHANNEL_POLICY not in prompt


def test_voice_channel_appends_policy_after_base_prompt() -> None:
    prompt = build_llm_system_prompt(_context(channel_policy="voice"))

    assert prompt.endswith(VOICE_CHANNEL_POLICY)
    assert prompt.startswith(_CHARACTER_PROMPT)


def test_default_preferred_builder_is_production_static_method() -> None:
    context = _context(preferred_expressions=["cheers"])

    prompt = build_llm_system_prompt(context)

    assert ChatLearningService.build_preferred_expressions_prompt(["cheers"]) in prompt


def test_generation_messages_layout_and_history_order() -> None:
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
    ]
    context = _context(history_messages=history, user_message="second")

    messages = build_generation_messages(
        context, user_message_builder=_build_user_message_with_images
    )

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "first"}
    assert messages[2] == {"role": "assistant", "content": "reply"}
    assert messages[3] == {"role": "user", "content": "second"}


def test_generation_messages_attach_images_via_canonical_builder() -> None:
    image_ref = ChatImageRef(
        image_key="chat_images/u1/img1",
        original="/media/chat_images/u1/img1/original",
        display="/media/chat_images/u1/img1/display",
    )
    context = _context(chat_images=(image_ref,), user_message="describe")

    messages = build_generation_messages(
        context, user_message_builder=_build_user_message_with_images
    )

    last = messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert last["content"][-1] == {"type": "text", "text": "describe"}
    assert any(part.get("type") == "image_url" for part in last["content"])
