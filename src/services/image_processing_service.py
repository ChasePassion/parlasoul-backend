"""Avatar image normalization and AVIF variant generation."""

from __future__ import annotations

import io
from dataclasses import dataclass
from functools import lru_cache

import anyio
from PIL import Image, ImageOps, UnidentifiedImageError, features

from src.core.config import Settings, settings
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError


@dataclass(frozen=True)
class ImageVariant:
    size: int
    body: bytes
    content_type: str = "image/avif"


@dataclass(frozen=True)
class ImageDimensions:
    width: int
    height: int


class ImageProcessingService:
    def __init__(self, config: Settings):
        self._settings = config

    def assert_avif_supported(self) -> None:
        if not features.check("avif"):
            raise APIError(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="internal error: Pillow AVIF support is unavailable",
            )

    async def build_avatar_variants(self, original_bytes: bytes) -> list[ImageVariant]:
        if len(original_bytes) > self._settings.MEDIA_MAX_UPLOAD_BYTES:
            limit_mb = max(1, self._settings.MEDIA_MAX_UPLOAD_BYTES // 1024 // 1024)
            raise APIError(
                status_code=413,
                code=ErrorCode.UPLOAD_FILE_TOO_LARGE,
                message=f"invalid upload: file size exceeds limit; limit_mb={limit_mb}",
            )

        return await anyio.to_thread.run_sync(self._build_avatar_variants_sync, original_bytes)

    async def convert_to_avif(self, original_bytes: bytes) -> bytes:
        """将任意格式图片转为 AVIF，不做缩放。"""
        if len(original_bytes) > self._settings.MEDIA_MAX_UPLOAD_BYTES:
            limit_mb = max(1, self._settings.MEDIA_MAX_UPLOAD_BYTES // 1024 // 1024)
            raise APIError(
                status_code=413,
                code=ErrorCode.UPLOAD_FILE_TOO_LARGE,
                message=f"invalid upload: file size exceeds limit; limit_mb={limit_mb}",
            )
        return await anyio.to_thread.run_sync(self._convert_to_avif_sync, original_bytes)

    async def build_display_variant(self, original_bytes: bytes, max_dimension: int = 1024) -> ImageVariant:
        """生成单个展示用 AVIF 变体（长边不超过 max_dimension）。"""
        if len(original_bytes) > self._settings.MEDIA_MAX_UPLOAD_BYTES:
            limit_mb = max(1, self._settings.MEDIA_MAX_UPLOAD_BYTES // 1024 // 1024)
            raise APIError(
                status_code=413,
                code=ErrorCode.UPLOAD_FILE_TOO_LARGE,
                message=f"invalid upload: file size exceeds limit; limit_mb={limit_mb}",
            )
        return await anyio.to_thread.run_sync(self._build_display_variant_sync, original_bytes, max_dimension)

    async def inspect_image(self, original_bytes: bytes) -> ImageDimensions:
        if len(original_bytes) > self._settings.MEDIA_MAX_UPLOAD_BYTES:
            limit_mb = max(1, self._settings.MEDIA_MAX_UPLOAD_BYTES // 1024 // 1024)
            raise APIError(
                status_code=413,
                code=ErrorCode.UPLOAD_FILE_TOO_LARGE,
                message=f"invalid upload: file size exceeds limit; limit_mb={limit_mb}",
            )

        return await anyio.to_thread.run_sync(self._inspect_image_sync, original_bytes)

    def _inspect_image_sync(self, original_bytes: bytes) -> ImageDimensions:
        try:
            with Image.open(io.BytesIO(original_bytes)) as source:
                source.load()
                normalized = ImageOps.exif_transpose(source)
                width, height = normalized.size
                if width <= 0 or height <= 0:
                    raise ValueError("invalid dimensions")
                if width * height > self._settings.MEDIA_IMAGE_MAX_PIXELS:
                    raise APIError(
                        status_code=422,
                        code=ErrorCode.VALIDATION_FAILED,
                        message=(
                            "validation failed: image pixel count exceeds limit; "
                            f"limit_pixels={self._settings.MEDIA_IMAGE_MAX_PIXELS}"
                        ),
                    )
                return ImageDimensions(width=width, height=height)
        except APIError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise APIError(
                status_code=415,
                code=ErrorCode.UPLOAD_FILE_TYPE_NOT_ALLOWED,
                message="invalid upload: image payload cannot be decoded",
            ) from exc

    def _convert_to_avif_sync(self, original_bytes: bytes) -> bytes:
        self.assert_avif_supported()
        try:
            with Image.open(io.BytesIO(original_bytes)) as source:
                source.load()
                normalized = ImageOps.exif_transpose(source)
                if normalized.mode not in ("RGB", "RGBA"):
                    normalized = normalized.convert("RGBA")
                if normalized.mode == "RGBA":
                    canvas = Image.new("RGB", normalized.size, (255, 255, 255))
                    canvas.paste(normalized, mask=normalized.getchannel("A"))
                    normalized = canvas
                else:
                    normalized = normalized.convert("RGB")
                output = io.BytesIO()
                normalized.save(
                    output,
                    format="AVIF",
                    quality=self._settings.MEDIA_IMAGE_QUALITY,
                )
                return output.getvalue()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise APIError(
                status_code=415,
                code=ErrorCode.UPLOAD_FILE_TYPE_NOT_ALLOWED,
                message="invalid upload: image payload cannot be decoded",
            ) from exc

    def _build_display_variant_sync(self, original_bytes: bytes, max_dimension: int) -> ImageVariant:
        self.assert_avif_supported()
        try:
            with Image.open(io.BytesIO(original_bytes)) as source:
                source.load()
                normalized = ImageOps.exif_transpose(source)
                if normalized.mode not in ("RGB", "RGBA"):
                    normalized = normalized.convert("RGBA")
                if normalized.mode == "RGBA":
                    canvas = Image.new("RGB", normalized.size, (255, 255, 255))
                    canvas.paste(normalized, mask=normalized.getchannel("A"))
                    normalized = canvas
                else:
                    normalized = normalized.convert("RGB")
                w, h = normalized.size
                ratio = min(max_dimension / max(w, h), 1.0)
                if ratio < 1.0:
                    new_size = (int(w * ratio), int(h * ratio))
                    normalized = normalized.resize(new_size, Image.LANCZOS)
                output = io.BytesIO()
                normalized.save(
                    output,
                    format="AVIF",
                    quality=self._settings.MEDIA_IMAGE_QUALITY,
                )
                return ImageVariant(size=max(normalized.size), body=output.getvalue())
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise APIError(
                status_code=415,
                code=ErrorCode.UPLOAD_FILE_TYPE_NOT_ALLOWED,
                message="invalid upload: image payload cannot be decoded",
            ) from exc

    def _build_avatar_variants_sync(self, original_bytes: bytes) -> list[ImageVariant]:
        self.assert_avif_supported()
        try:
            with Image.open(io.BytesIO(original_bytes)) as source:
                source.load()
                width, height = source.size
                if width <= 0 or height <= 0:
                    raise ValueError("invalid dimensions")
                if width * height > self._settings.MEDIA_IMAGE_MAX_PIXELS:
                    raise APIError(
                        status_code=422,
                        code=ErrorCode.VALIDATION_FAILED,
                        message=(
                            "validation failed: image pixel count exceeds limit; "
                            f"limit_pixels={self._settings.MEDIA_IMAGE_MAX_PIXELS}"
                        ),
                    )

                normalized = ImageOps.exif_transpose(source)
                if normalized.mode not in ("RGB", "RGBA"):
                    normalized = normalized.convert("RGBA")
                if normalized.mode == "RGBA":
                    canvas = Image.new("RGB", normalized.size, (255, 255, 255))
                    canvas.paste(normalized, mask=normalized.getchannel("A"))
                    normalized = canvas
                else:
                    normalized = normalized.convert("RGB")

                variants: list[ImageVariant] = []
                for size in self._settings.media_image_variants_list:
                    fitted = ImageOps.fit(
                        normalized,
                        (size, size),
                        method=Image.Resampling.LANCZOS,
                        centering=(0.5, 0.5),
                    )
                    output = io.BytesIO()
                    fitted.save(
                        output,
                        format="AVIF",
                        quality=self._settings.MEDIA_IMAGE_QUALITY,
                    )
                    variants.append(ImageVariant(size=size, body=output.getvalue()))
                return variants
        except APIError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise APIError(
                status_code=415,
                code=ErrorCode.UPLOAD_FILE_TYPE_NOT_ALLOWED,
                message="invalid upload: image payload cannot be decoded",
            ) from exc


@lru_cache
def get_image_processing_service() -> ImageProcessingService:
    return ImageProcessingService(settings)
