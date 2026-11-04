from src.schemas.media import MediaUploadKind
from src.services.media_url_service import (
    build_chat_image_ref,
    extract_chat_image_key_from_url,
    normalize_chat_image_key,
    public_media_content_type,
    validate_avatar_image_key_owner,
    validate_public_media_object_key,
)


def test_validate_avatar_image_key_owner_accepts_matching_owner_and_kind() -> None:
    assert (
        validate_avatar_image_key_owner(
            "/images/avatars/characters/user-1/image-1/",
            owner_id="user-1",
            allowed_kinds=(MediaUploadKind.CHARACTER_AVATAR,),
        )
        == "images/avatars/characters/user-1/image-1"
    )


def test_validate_avatar_image_key_owner_rejects_wrong_owner() -> None:
    assert (
        validate_avatar_image_key_owner(
            "images/avatars/characters/user-2/image-1",
            owner_id="user-1",
            allowed_kinds=(MediaUploadKind.CHARACTER_AVATAR,),
        )
        is None
    )


def test_validate_avatar_image_key_owner_rejects_wrong_kind() -> None:
    assert (
        validate_avatar_image_key_owner(
            "images/avatars/voices/user-1/image-1",
            owner_id="user-1",
            allowed_kinds=(MediaUploadKind.CHARACTER_AVATAR,),
        )
        is None
    )


def test_validate_avatar_image_key_owner_rejects_variant_or_traversal() -> None:
    assert (
        validate_avatar_image_key_owner(
            "images/avatars/characters/user-1/image-1/192.avif",
            owner_id="user-1",
            allowed_kinds=(MediaUploadKind.CHARACTER_AVATAR,),
        )
        is None
    )


def test_public_media_object_key_accepts_discover_hero_images() -> None:
    assert (
        validate_public_media_object_key("/images/discover/heroes/elon-21-9.jpg")
        == "images/discover/heroes/elon-21-9.jpg"
    )
    assert (
        validate_public_media_object_key("images/discover/heroes/../private.jpg")
        is None
    )
    assert (
        validate_public_media_object_key("images/discover/heroes/nested/private.jpg")
        is None
    )
    assert public_media_content_type("images/discover/heroes/elon-21-9.jpg") == "image/jpeg"
    assert (
        validate_avatar_image_key_owner(
            "images/avatars/characters/user-1/../image-1",
            owner_id="user-1",
            allowed_kinds=(MediaUploadKind.CHARACTER_AVATAR,),
        )
        is None
    )


def test_normalize_chat_image_key_accepts_expected_prefix() -> None:
    assert (
        normalize_chat_image_key("/images/avatars/chat/user-1/image-1/")
        == "images/avatars/chat/user-1/image-1"
    )


def test_build_chat_image_ref_uses_media_urls() -> None:
    ref = build_chat_image_ref("images/avatars/chat/user-1/image-1")

    assert ref is not None
    assert ref.image_key == "images/avatars/chat/user-1/image-1"
    assert ref.original == "/media/images/avatars/chat/user-1/image-1/original"
    assert ref.display == "/media/images/avatars/chat/user-1/image-1/display.avif"


def test_extract_chat_image_key_from_url_supports_legacy_original_and_display_urls() -> None:
    assert (
        extract_chat_image_key_from_url("https://cdn.example.com/images/avatars/chat/user-1/image-1/original")
        == "images/avatars/chat/user-1/image-1"
    )
    assert (
        extract_chat_image_key_from_url("/media/images/avatars/chat/user-1/image-1/display.avif")
        == "images/avatars/chat/user-1/image-1"
    )
