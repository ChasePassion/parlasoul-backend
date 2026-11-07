"""Generate character avatars and persist their AVIF variants to R2."""

from __future__ import annotations

import io
import logging
import uuid

import anyio
from PIL import Image, ImageOps

from src.core.exceptions import APIError
from src.repositories.image_generation_repository import ImageGenerationRepository
from src.schemas.media import AvatarUrls
from src.services.image_processing_service import ImageProcessingService
from src.services.media_url_service import build_avatar_urls, variant_object_key
from src.services.r2_storage_service import R2StorageService

logger = logging.getLogger(__name__)

CACHE_CONTROL_IMMUTABLE = "public, max-age=31536000, immutable"
_AVATAR_SOURCE_PIXELS = 1024


def _prepare_source_image(raw: bytes) -> bytes:
    """Center-crop to a square and re-encode as JPEG before variant generation.

    Qwen-Image returns a 2048x2048 PNG that can exceed the 5 MB upload limit.
    Avatars are only served up to 768 px, so one 1024 px JPEG is a lossless-enough
    source for every downstream variant.
    """
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            normalized = ImageOps.exif_transpose(source).convert("RGB")
            side = min(normalized.size)
            left = (normalized.width - side) // 2
            top = (normalized.height - side) // 2
            square = normalized.crop((left, top, left + side, top + side))
            square = square.resize(
                (_AVATAR_SOURCE_PIXELS, _AVATAR_SOURCE_PIXELS),
                Image.LANCZOS,
            )
            output = io.BytesIO()
            square.save(output, format="JPEG", quality=92, optimize=True)
            return output.getvalue()
    except (OSError, ValueError) as exc:
        raise APIError(
            status_code=502,
            code="external_error",
            message="upstream error: generated avatar cannot be decoded",
        ) from exc


class AvatarGenerationService:
    def __init__(
        self,
        *,
        image_repository: ImageGenerationRepository,
        image_processor: ImageProcessingService,
        r2_storage: R2StorageService,
    ):
        self._images = image_repository
        self._processor = image_processor
        self._r2 = r2_storage

    async def generate(
        self,
        *,
        user_id: uuid.UUID,
        prompt: str,
        reference_images: list[str] | None = None,
    ) -> tuple[str, AvatarUrls]:
        raw = await self._images.generate(
            prompt=prompt, reference_images=reference_images
        )
        prepared = await anyio.to_thread.run_sync(_prepare_source_image, raw)
        variants = await self._processor.build_avatar_variants(prepared)

        image_key = f"images/avatars/characters/{user_id}/{uuid.uuid4().hex}"
        for variant in variants:
            await self._r2.put_object(
                object_key=variant_object_key(image_key, variant.size),
                body=variant.body,
                content_type=variant.content_type,
                cache_control=CACHE_CONTROL_IMMUTABLE,
            )

        avatar_urls = build_avatar_urls(image_key)
        if avatar_urls is None:
            raise APIError(
                status_code=500,
                code="internal_error",
                message="internal error: generated avatar has no media key",
            )
        logger.info(
            "generated character avatar: user_id=%s image_key=%s variants=%d",
            user_id,
            image_key,
            len(variants),
        )
        return image_key, avatar_urls
