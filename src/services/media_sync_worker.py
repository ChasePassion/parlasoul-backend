"""Background mirroring of locally staged media into R2.

Uploads answer as soon as the bytes are durable on the host, so the object
store write happens here instead of inside the request. The file system keeps
pending work, which means a restart only needs a rescan - no queue service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import lru_cache

import anyio

from src.core.config import Settings, settings
from src.core.exceptions import APIError
from src.services.local_media_store import LocalMediaStore, get_local_media_store
from src.services.r2_storage_service import R2StorageService, get_r2_storage_service

logger = logging.getLogger(__name__)


class MediaSyncWorker:
    """Copies locally staged objects to R2 and tracks their sync state."""

    def __init__(
        self,
        *,
        local_store: LocalMediaStore,
        r2_storage: R2StorageService,
        config: Settings,
    ):
        self._store = local_store
        self._r2_storage = r2_storage
        self._settings = config
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1024)
        self._tasks: list[asyncio.Task[None]] = []
        self.synced_count = 0
        self.failed_count = 0
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        if self._running or not self._settings.LOCAL_MEDIA_SYNC_ENABLED:
            return
        self._running = True
        for index in range(max(1, self._settings.LOCAL_MEDIA_SYNC_WORKER_COUNT)):
            self._tasks.append(
                asyncio.create_task(self._worker_loop(), name=f"media_sync_worker_{index + 1}")
            )
        self._tasks.append(
            asyncio.create_task(self._scan_loop(), name="media_sync_scan")
        )
        self._tasks.append(
            asyncio.create_task(self._gc_loop(), name="media_sync_gc")
        )
        pending = await anyio.to_thread.run_sync(self._store.pending_keys)
        for object_key in pending:
            self.enqueue(object_key)
        logger.info(
            "media sync worker started: pending=%d workers=%d",
            len(pending),
            self._settings.LOCAL_MEDIA_SYNC_WORKER_COUNT,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        logger.info(
            "media sync worker stopped: synced=%d failed=%d",
            self.synced_count,
            self.failed_count,
        )

    def enqueue(self, object_key: str) -> None:
        """Queue one object; a full queue falls back to the periodic rescan."""

        if not self._settings.LOCAL_MEDIA_SYNC_ENABLED:
            return
        try:
            self._queue.put_nowait(object_key)
        except asyncio.QueueFull:
            logger.warning("media sync queue is full; relying on rescan: key=%s", object_key)

    async def _worker_loop(self) -> None:
        while True:
            object_key = await self._queue.get()
            try:
                await self._sync_one(object_key)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive guard for the loop
                self.failed_count += 1
                logger.exception("media sync crashed on key=%s", object_key)
            finally:
                self._queue.task_done()

    async def _sync_one(self, object_key: str) -> bool:
        stored = await anyio.to_thread.run_sync(self._store.get, object_key)
        if stored is None:
            logger.warning("media sync skipped missing local object: key=%s", object_key)
            return False
        try:
            await self._r2_storage.put_object(
                object_key=object_key,
                body=stored.body,
                content_type=stored.content_type,
                cache_control=stored.cache_control,
            )
        except APIError:
            self.failed_count += 1
            logger.warning(
                "media sync failed, will retry on rescan: key=%s bytes=%d",
                object_key,
                len(stored.body),
            )
            return False
        await anyio.to_thread.run_sync(self._store.mark_synced, object_key)
        self.synced_count += 1
        logger.info("media synced to r2: key=%s bytes=%d", object_key, len(stored.body))
        return True

    async def _scan_loop(self) -> None:
        interval = max(5, self._settings.LOCAL_MEDIA_SYNC_SCAN_SECONDS)
        while True:
            await asyncio.sleep(interval)
            try:
                pending = await anyio.to_thread.run_sync(self._store.pending_keys)
                for object_key in pending:
                    self.enqueue(object_key)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - keep the scanner alive
                logger.exception("media sync rescan failed")

    async def _gc_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                removed = await anyio.to_thread.run_sync(
                    lambda: self._store.collect_garbage(
                        retention_hours=self._settings.LOCAL_MEDIA_RETENTION_HOURS
                    )
                )
                if removed:
                    logger.info("media local cache collected: removed=%d", removed)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - keep the collector alive
                logger.exception("media local cache collection failed")


@lru_cache
def get_media_sync_worker() -> MediaSyncWorker:
    return MediaSyncWorker(
        local_store=get_local_media_store(),
        r2_storage=get_r2_storage_service(),
        config=settings,
    )
