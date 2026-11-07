"""Character-forge image transport."""

from __future__ import annotations

from src.schemas.character_forge import ForgeStreamRequest
from src.schemas.chat import ChatImageRef

_INLINE = "data:image/jpeg;base64,AAAA"


def _ref(data_url: str | None = None) -> ChatImageRef:
    return ChatImageRef(
        image_key="chat-images/user-1/pic.jpg",
        original="https://example.test/original.jpg",
        display="https://example.test/display.jpg",
        data_url=data_url,
    )


def test_forge_stream_request_accepts_images():
    request = ForgeStreamRequest(content="hi", images=[_ref(_INLINE)])

    assert request.images is not None
    assert request.images[0].data_url == _INLINE
