from src.services.chat_generation_service import (
    VOICE_CHANNEL_POLICY,
    ChatGenerationContext,
    build_generation_messages,
    build_llm_system_prompt,
    strip_image_parts,
)


def _voice_context(character_prompt: str, history_messages: list | None = None) -> ChatGenerationContext:
    return ChatGenerationContext(
        character_system_prompt=character_prompt,
        dialogue_style_id=None,
        history_messages=history_messages or [],
        user_message="hello",
        memories={"episodic": [], "semantic": []},
        use_all_semantic=False,
        preferred_expressions=[],
        memory_active=False,
        channel_policy="voice",
    )


def _user_message_builder(text: str, chat_images: list | None) -> dict:
    if chat_images:
        raise AssertionError("voice channel must not attach images")
    return {"role": "user", "content": text}


def test_voice_channel_policy_keeps_character_prompt_then_adds_voice_rules() -> None:
    prompt = build_llm_system_prompt(_voice_context("Stay in character."))

    assert prompt.startswith("Stay in character.\n")
    assert prompt.endswith(VOICE_CHANNEL_POLICY)
    assert "Do not use Markdown" in prompt


def test_voice_channel_strips_image_parts_keeping_text_on_same_message() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                {"type": "text", "text": "看看这张图"},
            ],
        }
    ]

    result = strip_image_parts(messages)

    assert result == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看看这张图"},
            ],
        }
    ]


def test_voice_channel_strips_pure_image_messages() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            ],
        }
    ]

    result = strip_image_parts(messages)

    assert result == []


def test_voice_channel_keeps_plain_text_messages_unchanged() -> None:
    messages = [
        {"role": "assistant", "content": "这是纯文本回复"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "第一段"},
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                {"type": "text", "text": "第二段"},
            ],
        },
    ]

    result = strip_image_parts(messages)

    assert result == [
        {"role": "assistant", "content": "这是纯文本回复"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "第一段"},
                {"type": "text", "text": "第二段"},
            ],
        },
    ]


def test_voice_generation_messages_strip_history_and_send_plain_user_message() -> None:
    context = _voice_context(
        "Stay in character.",
        history_messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                    {"type": "text", "text": "图片消息"},
                ],
            },
            {"role": "assistant", "content": "回复"},
        ],
    )

    messages = build_generation_messages(context, user_message_builder=_user_message_builder)

    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": [{"type": "text", "text": "图片消息"}]}
    assert messages[-1] == {"role": "user", "content": "hello"}
