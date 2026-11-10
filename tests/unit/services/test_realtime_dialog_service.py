from src.services.chat_generation_service import (
    ChatGenerationContext,
    strip_image_parts,
)


def _voice_context(character_prompt: str, history_messages: list | None = None) -> ChatGenerationContext:
    return ChatGenerationContext(
        character_system_prompt=character_prompt,
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


