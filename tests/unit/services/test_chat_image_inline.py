"""Inline base64 image parts for character chat messages."""

from __future__ import annotations

from src.schemas.chat import ChatImageRef
from src.services import chat_service
from src.services.chat_service import (
    _build_user_message_with_images,
    _materialize_chat_image_refs,
)

_INLINE = "data:image/jpeg;base64,AAAA"


def _ref(data_url: str | None = None) -> ChatImageRef:
    return ChatImageRef(
        image_key="chat-images/user-1/pic.jpg",
        original="https://example.test/original.jpg",
        display="https://example.test/display.jpg",
        data_url=data_url,
    )


def test_inline_data_url_is_used_verbatim():
    message = _build_user_message_with_images("look", [_ref(_INLINE)])

    parts = message["content"]
    assert isinstance(parts, list)
    assert parts[0] == {"type": "image_url", "image_url": {"url": _INLINE}}
    assert parts[-1] == {"type": "text", "text": "look"}


def test_missing_data_url_falls_back_to_generated_url(monkeypatch):
    monkeypatch.setattr(
        chat_service, "_llm_image_url", lambda key: f"https://cdn.test/{key}"
    )

    message = _build_user_message_with_images("look", [_ref()])

    assert message["content"][0]["image_url"]["url"] == (
        "https://cdn.test/chat-images/user-1/pic.jpg"
    )


def test_materialize_keeps_inline_data_url(monkeypatch):
    monkeypatch.setattr(chat_service, "build_chat_image_ref", lambda key: _ref())

    refs = _materialize_chat_image_refs(
        [{"image_key": "chat-images/user-1/pic.jpg", "data_url": _INLINE}]
    )

    assert refs is not None
    assert refs[0].data_url == _INLINE
