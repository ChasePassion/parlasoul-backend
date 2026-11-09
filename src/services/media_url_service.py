"""Media object key and public URL helpers."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse

from src.core.config import settings
from src.schemas.chat import ChatImageRef
from src.schemas.media import AvatarUrls, MediaUploadKind

_KIND_PATH_SEGMENT = {
    MediaUploadKind.USER_AVATAR: "users",
    MediaUploadKind.CHARACTER_AVATAR: "characters",
    MediaUploadKind.VOICE_AVATAR: "voices",
    MediaUploadKind.CHAT_IMAGE: "chat",
}
DISCOVER_HERO_PREFIX = "images/discover/heroes/"
DISCOVER_HERO_CONTENT_TYPES = {
    ".avif": "image/avif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def avatar_image_key(kind: MediaUploadKind, owner_id: str, image_id: str) -> str:
    return f"images/avatars/{_KIND_PATH_SEGMENT[kind]}/{owner_id}/{image_id}"


def validate_avatar_image_key_owner(
    image_key: str | None,
    *,
    owner_id: str,
    allowed_kinds: Iterable[MediaUploadKind],
) -> str | None:
    if image_key is None:
        return None

    normalized = image_key.strip().strip("/")
    if not normalized:
        return None
    if ".." in normalized.split("/"):
        return None

    for kind in allowed_kinds:
        expected_prefix = avatar_image_key(kind, owner_id, "")
        if not normalized.startswith(expected_prefix):
            continue
        remainder = normalized[len(expected_prefix) :]
        if remainder and "/" not in remainder:
            return normalized
    return None


def normalize_chat_image_key(image_key: str | None) -> str | None:
    if image_key is None:
        return None

    normalized = image_key.strip().strip("/")
    if not normalized:
        return None
    if ".." in normalized.split("/"):
        return None
    prefix = f"images/avatars/{_KIND_PATH_SEGMENT[MediaUploadKind.CHAT_IMAGE]}/"
    if not normalized.startswith(prefix):
        return None
    remainder = normalized[len(prefix) :]
    if not remainder:
        return None
    owner_id, separator, image_id = remainder.partition("/")
    if not (separator and owner_id and image_id and "/" not in image_id):
        return None
    return normalized


def original_object_key(image_key: str) -> str:
    return f"{image_key}/original"


CHAT_AUDIO_PREFIX = "audios/chat/"


def chat_audio_key(owner_id: str, audio_id: str) -> str:
    return f"{CHAT_AUDIO_PREFIX}{owner_id}/{audio_id}"


def validate_chat_audio_key_owner(audio_key: str | None, *, owner_id: str) -> str | None:
    """Return the normalized audio key only when it belongs to ``owner_id``."""

    if audio_key is None:
        return None

    normalized = audio_key.strip().strip("/")
    if not normalized or ".." in normalized.split("/"):
        return None
    expected_prefix = chat_audio_key(owner_id, "")
    if not normalized.startswith(expected_prefix):
        return None
    remainder = normalized[len(expected_prefix) :]
    if not remainder or "/" in remainder:
        return None
    return normalized


def variant_object_key(image_key: str, size: int) -> str:
    return f"{image_key}/{size}.avif"


def build_media_url(object_key: str) -> str:
    return f"{settings.media_public_base_url_normalized}/{object_key.lstrip('/')}"


def build_r2_public_url(object_key: str) -> str | None:
    base_url = (settings.R2_PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base_url:
        return None
    return f"{base_url}/{object_key.lstrip('/')}"


def discover_hero_object_key(image_id: str, extension: str) -> str:
    normalized_extension = extension.lower()
    if not normalized_extension.startswith("."):
        normalized_extension = f".{normalized_extension}"
    return f"{DISCOVER_HERO_PREFIX}{image_id}{normalized_extension}"


def build_avatar_urls(image_key: str | None) -> AvatarUrls | None:
    if not image_key:
        return None

    configured = sorted(settings.media_image_variants_list)
    sm = 96 if 96 in configured else configured[0]
    md = 192 if 192 in configured else min(configured, key=lambda value: abs(value - 192))
    lg = 512 if 512 in configured else configured[-1]
    xl = 768 if 768 in configured else lg

    return AvatarUrls(
        sm=build_media_url(variant_object_key(image_key, sm)),
        md=build_media_url(variant_object_key(image_key, md)),
        lg=build_media_url(variant_object_key(image_key, lg)),
        xl=build_media_url(variant_object_key(image_key, xl)),
    )


def validate_discover_hero_object_key(object_key: str) -> str | None:
    normalized = object_key.strip().lstrip("/")
    if not normalized:
        return None
    if ".." in normalized.split("/"):
        return None
    if not normalized.startswith(DISCOVER_HERO_PREFIX):
        return None
    if "/" in normalized.removeprefix(DISCOVER_HERO_PREFIX):
        return None

    lowered = normalized.lower()
    for extension in DISCOVER_HERO_CONTENT_TYPES:
        if lowered.endswith(extension):
            return normalized
    return None


def validate_public_media_object_key(object_key: str) -> str | None:
    normalized = object_key.strip().lstrip("/")
    if not normalized:
        return None
    if ".." in normalized.split("/"):
        return None
    discover_hero_key = validate_discover_hero_object_key(normalized)
    if discover_hero_key is not None:
        return discover_hero_key
    if normalized.startswith("images/avatars/") and normalized.endswith(".avif"):
        return normalized
    if normalized.startswith("voices/preview/") and normalized.endswith(".mp3"):
        return normalized
    if normalized.startswith("images/avatars/chat/") and not normalized.endswith("/"):
        return normalized
    return None


def public_media_content_type(object_key: str) -> str:
    lowered = object_key.lower()
    for extension, content_type in DISCOVER_HERO_CONTENT_TYPES.items():
        if lowered.endswith(extension):
            return content_type
    if lowered.endswith(".wav"):
        return "audio/wav"
    if lowered.endswith(".mp3"):
        return "audio/mpeg"
    if lowered.endswith(".opus"):
        return "audio/opus"
    return "image/avif"


def chat_image_display_object_key(image_key: str) -> str:
    return f"{image_key}/display.avif"


def build_chat_image_urls(image_key: str, original_ext: str = "") -> dict[str, str]:
    original_key = f"{image_key}/original{original_ext}"
    display_key = chat_image_display_object_key(image_key)
    return {
        "original": build_media_url(original_key),
        "display": build_media_url(display_key),
    }


def build_chat_image_ref(image_key: str) -> ChatImageRef | None:
    normalized = normalize_chat_image_key(image_key)
    if normalized is None:
        return None
    urls = build_chat_image_urls(normalized)
    return ChatImageRef(
        image_key=normalized,
        original=urls["original"],
        display=urls["display"],
    )


def extract_chat_image_key_from_url(url: str | None) -> str | None:
    if not url:
        return None

    parsed = urlparse(url)
    path = (parsed.path or url).strip().lstrip("/")
    if not path:
        return None

    media_root = settings.media_public_base_url_normalized.lstrip("/")
    if media_root and path.startswith(f"{media_root}/"):
        path = path[len(media_root) + 1 :]

    if path.endswith("/display.avif"):
        return normalize_chat_image_key(path.removesuffix("/display.avif"))
    if "/original" in path:
        return normalize_chat_image_key(path.rsplit("/original", 1)[0])
    return None
