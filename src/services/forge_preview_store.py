"""Redis-backed state for character-forge voice previews.

The forge agent used to echo a 512-character signed token back into
``create_character``. A model rewrites that token whenever it carries prose:
it re-types the embedded sample text and "fixes" the typography, so the HMAC
no longer matches and the tool call fails. The preview therefore lives in
Redis behind a short session-scoped serial number, and nothing opaque ever
travels back through the model.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.core.config import Settings, settings
from src.core.exceptions import APIError

logger = logging.getLogger(__name__)

PREVIEW_TTL_SECONDS = 60 * 60 * 6


def normalize_preview_handle(raw: str) -> str | None:
    """Fold a written handle into its canonical ``pv<n>`` form.

    Handles are short enough that a model rarely garbles them, but a stray
    space, a capital letter or a padded number should not cost the user a
    re-render.
    """
    folded = re.sub(r"[^a-z0-9]", "", (raw or "").strip().lower())
    if not folded.startswith("pv"):
        return None
    digits = folded[2:]
    if not digits.isdigit():
        return None
    return f"pv{int(digits)}"


class ForgePreviewStore:
    """Short-lived voice-preview payloads keyed by session-scoped handles."""

    def __init__(self, config: Settings | None = None) -> None:
        self._settings = config or settings
        self._client: Redis | None = None

    def _redis_url(self) -> str:
        url = (self._settings.REDIS_URL or "").strip()
        if not url:
            raise APIError(
                status_code=500,
                code="internal_error",
                message="internal error: redis is not configured for voice previews",
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

    async def create(
        self,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> str:
        """Store one preview and return the handle that addresses it."""
        try:
            seq = await self.client.incr(f"forge:preview:seq:{session_id}")
            handle = f"pv{seq}"
            record = json.dumps(
                {"user_id": str(user_id), "payload": payload},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            pipe = self.client.pipeline()
            pipe.set(
                f"forge:preview:{session_id}:{handle}",
                record,
                ex=PREVIEW_TTL_SECONDS,
            )
            pipe.sadd(f"forge:preview:index:{session_id}", handle)
            pipe.expire(f"forge:preview:index:{session_id}", PREVIEW_TTL_SECONDS)
            await pipe.execute()
            return handle
        except RedisError as exc:
            logger.warning("voice preview store write failed: %s", exc)
            raise APIError(
                status_code=502,
                code="external_error",
                message="upstream error: voice preview store is unavailable",
            ) from exc

    async def get(
        self,
        *,
        session_id: uuid.UUID,
        handle: str,
        user_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Return the preview payload behind a handle owned by this user."""
        canonical = normalize_preview_handle(handle)
        if canonical is None:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: invalid voice preview id",
            )
        try:
            raw = await self.client.get(f"forge:preview:{session_id}:{canonical}")
        except RedisError as exc:
            logger.warning("voice preview store read failed: %s", exc)
            raise APIError(
                status_code=502,
                code="external_error",
                message="upstream error: voice preview store is unavailable",
            ) from exc
        if raw is None:
            raise APIError(
                status_code=422,
                code="validation_failed",
                message="validation failed: voice preview not found",
            )
        record = json.loads(raw)
        if record.get("user_id") != str(user_id):
            raise APIError(
                status_code=403,
                code="forbidden",
                message="authorization failed: voice preview belongs to another user",
            )
        return record["payload"]


__all__ = [
    "PREVIEW_TTL_SECONDS",
    "ForgePreviewStore",
    "normalize_preview_handle",
]
