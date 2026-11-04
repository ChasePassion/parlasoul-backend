"""Redis-backed short-lived media state and hot bytes cache."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core.config import Settings, settings
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError


@dataclass(frozen=True)
class CachedMediaObject:
    body: bytes
    content_type: str
    etag: str | None
    cache_control: str


class MediaCacheService:
    def __init__(self, config: Settings):
        self._settings = config
        self._client: Redis | None = None

    def _redis_url(self) -> str:
        url = (self._settings.REDIS_URL or "").strip()
        if not url:
            raise APIError(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message="internal error: redis is not configured for media storage",
            )
        return url

    @property
    def client(self) -> Redis:
        if self._client is None:
            self._client = Redis.from_url(
                self._redis_url(),
                decode_responses=False,
                socket_connect_timeout=15,
                socket_keepalive=True,
            )
        return self._client

    async def set_upload_session(self, upload_id: str, payload: dict[str, Any], ttl: int) -> None:
        try:
            await self.client.set(
                f"media:upload:{upload_id}",
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
                ex=ttl,
            )
        except RedisError as exc:
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to persist media upload session",
            ) from exc

    async def get_upload_session(self, upload_id: str) -> dict[str, Any] | None:
        try:
            raw = await self.client.get(f"media:upload:{upload_id}")
        except RedisError as exc:
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to read media upload session",
            ) from exc
        if raw is None:
            return None
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(
                status_code=409,
                code=ErrorCode.RESOURCE_CONFLICT,
                message="resource conflict: media upload session is corrupted",
            ) from exc
        return data if isinstance(data, dict) else None

    async def acquire_upload_lock(self, upload_id: str, ttl: int = 60) -> bool:
        try:
            return bool(
                await self.client.set(
                    f"media:upload-lock:{upload_id}",
                    b"1",
                    ex=ttl,
                    nx=True,
                )
            )
        except RedisError as exc:
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to lock media upload session",
            ) from exc

    async def release_upload_lock(self, upload_id: str) -> None:
        try:
            await self.client.delete(f"media:upload-lock:{upload_id}")
        except RedisError:
            return

    async def mark_upload_completed(
        self,
        upload_id: str,
        payload: dict[str, Any],
        ttl: int,
    ) -> None:
        next_payload = {**payload, "status": "completed"}
        await self.set_upload_session(upload_id, next_payload, ttl)

    def _object_hash(self, object_key: str) -> str:
        return sha256(object_key.encode("utf-8")).hexdigest()

    async def get_cached_media(self, object_key: str) -> CachedMediaObject | None:
        key_hash = self._object_hash(object_key)
        try:
            blob, meta_raw = await self.client.mget(
                f"media:blob:{key_hash}",
                f"media:meta:{key_hash}",
            )
        except RedisError as exc:
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to read media cache",
            ) from exc
        if blob is None or meta_raw is None:
            return None
        try:
            meta_text = meta_raw.decode("utf-8") if isinstance(meta_raw, bytes) else str(meta_raw)
            meta = json.loads(meta_text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(meta, dict):
            return None
        return CachedMediaObject(
            body=bytes(blob),
            content_type=str(meta.get("content_type") or "application/octet-stream"),
            etag=meta.get("etag") if isinstance(meta.get("etag"), str) else None,
            cache_control=str(meta.get("cache_control") or "public, max-age=31536000, immutable"),
        )

    async def set_cached_media(
        self,
        *,
        object_key: str,
        body: bytes,
        content_type: str,
        etag: str | None,
        cache_control: str,
    ) -> None:
        if len(body) > self._settings.MEDIA_CACHE_MAX_BYTES:
            return

        key_hash = self._object_hash(object_key)
        ttl = self._settings.MEDIA_CACHE_TTL_SECONDS
        meta = {
            "content_type": content_type,
            "etag": etag,
            "cache_control": cache_control,
        }
        try:
            pipe = self.client.pipeline()
            pipe.set(f"media:blob:{key_hash}", body, ex=ttl)
            pipe.set(
                f"media:meta:{key_hash}",
                json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
                ex=ttl,
            )
            await pipe.execute()
        except RedisError:
            return

    async def set_negative_media(self, object_key: str) -> None:
        try:
            await self.client.set(
                f"media:miss:{self._object_hash(object_key)}",
                b"1",
                ex=self._settings.MEDIA_CACHE_NEGATIVE_TTL_SECONDS,
            )
        except RedisError:
            return

    async def has_negative_media(self, object_key: str) -> bool:
        try:
            return bool(await self.client.exists(f"media:miss:{self._object_hash(object_key)}"))
        except RedisError:
            return False


@lru_cache
def get_media_cache_service() -> MediaCacheService:
    return MediaCacheService(settings)
