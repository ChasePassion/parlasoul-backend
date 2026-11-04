"""Cloudflare R2 storage adapter using the S3-compatible API."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import anyio
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.core.config import Settings, settings
from src.core.error_codes import ErrorCode
from src.core.exceptions import APIError


@dataclass(frozen=True)
class R2Object:
    body: bytes
    content_type: str
    etag: str | None
    cache_control: str | None


class R2StorageService:
    def __init__(self, config: Settings):
        self._settings = config
        self._client: Any | None = None

    def _require_configured(self) -> None:
        missing = [
            name
            for name, value in {
                "R2_ACCESS_KEY_ID": self._settings.R2_ACCESS_KEY_ID,
                "R2_SECRET_ACCESS_KEY": self._settings.R2_SECRET_ACCESS_KEY,
                "R2_ENDPOINT_URL": self._settings.R2_ENDPOINT_URL,
                "R2_BUCKET_NAME": self._settings.R2_BUCKET_NAME,
            }.items()
            if not value
        ]
        if missing:
            raise APIError(
                status_code=500,
                code=ErrorCode.INTERNAL_ERROR,
                message=f"internal error: media storage is not configured; missing={','.join(missing)}",
            )

    @property
    def client(self):
        self._require_configured()
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self._settings.R2_ENDPOINT_URL,
                aws_access_key_id=self._settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=self._settings.R2_SECRET_ACCESS_KEY,
                region_name=self._settings.R2_REGION,
                config=Config(signature_version="s3v4"),
            )
        return self._client

    async def generate_presigned_put_url(
        self,
        *,
        object_key: str,
        content_type: str,
        expires_in_seconds: int,
    ) -> str:
        try:
            return await anyio.to_thread.run_sync(
                lambda: self.client.generate_presigned_url(
                    "put_object",
                    Params={
                        "Bucket": self._settings.R2_BUCKET_NAME,
                        "Key": object_key,
                        "ContentType": content_type,
                    },
                    ExpiresIn=expires_in_seconds,
                )
            )
        except (BotoCoreError, ClientError) as exc:
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to create media upload URL",
            ) from exc

    async def put_object(
        self,
        *,
        object_key: str,
        body: bytes,
        content_type: str,
        cache_control: str,
    ) -> str | None:
        try:
            response = await anyio.to_thread.run_sync(
                lambda: self.client.put_object(
                    Bucket=self._settings.R2_BUCKET_NAME,
                    Key=object_key,
                    Body=body,
                    ContentType=content_type,
                    CacheControl=cache_control,
                )
            )
            return response.get("ETag")
        except (BotoCoreError, ClientError) as exc:
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to write media object",
            ) from exc

    async def head_object(self, *, object_key: str) -> dict | None:
        try:
            response = await anyio.to_thread.run_sync(
                lambda: self.client.head_object(
                    Bucket=self._settings.R2_BUCKET_NAME,
                    Key=object_key,
                )
            )
            return response
        except ClientError as exc:
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status_code == 404:
                return None
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to read media metadata",
            ) from exc
        except BotoCoreError as exc:
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to read media metadata",
            ) from exc

    async def get_object(self, *, object_key: str) -> R2Object:
        try:
            response = await anyio.to_thread.run_sync(
                lambda: self.client.get_object(
                    Bucket=self._settings.R2_BUCKET_NAME,
                    Key=object_key,
                )
            )
            body = await anyio.to_thread.run_sync(response["Body"].read)
        except ClientError as exc:
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status_code == 404:
                raise APIError(
                    status_code=404,
                    code=ErrorCode.RESOURCE_NOT_FOUND,
                    message="resource not found: media object does not exist",
                ) from exc
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to read media object",
            ) from exc
        except BotoCoreError as exc:
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to read media object",
            ) from exc

        return R2Object(
            body=body,
            content_type=str(response.get("ContentType") or "application/octet-stream"),
            etag=response.get("ETag"),
            cache_control=response.get("CacheControl"),
        )

    async def delete_object(self, *, object_key: str) -> None:
        try:
            await anyio.to_thread.run_sync(
                lambda: self.client.delete_object(
                    Bucket=self._settings.R2_BUCKET_NAME,
                    Key=object_key,
                )
            )
        except (BotoCoreError, ClientError) as exc:
            raise APIError(
                status_code=502,
                code=ErrorCode.EXTERNAL_ERROR,
                message="upstream error: failed to delete media object",
            ) from exc


@lru_cache
def get_r2_storage_service() -> R2StorageService:
    return R2StorageService(settings)
