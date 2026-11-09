"""Durable local staging area for user media that R2 stores asynchronously.

Uploads land here first so a user never waits for a cross-border object write.
A background worker mirrors the objects to R2; the file system itself is the
queue, which keeps pending work alive across restarts without adding another
service.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from src.core.config import settings

logger = logging.getLogger(__name__)

_OBJECTS_DIR = "objects"
_META_DIR = "meta"
_META_SUFFIX = ".json"


@dataclass(frozen=True)
class LocalMediaObject:
    body: bytes
    content_type: str
    cache_control: str


def _safe_relative_key(object_key: str) -> Path | None:
    normalized = object_key.strip().lstrip("/")
    if not normalized:
        return None
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return Path(*parts)


class LocalMediaStore:
    """File-system store for freshly uploaded media.

    Layout:
        <root>/objects/<object_key>      object bytes
        <root>/meta/<object_key>.json    content type, cache control, sync state
    """

    def __init__(self, root: str | Path):
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def _object_path(self, key: Path) -> Path:
        return self._root / _OBJECTS_DIR / key

    def _meta_path(self, key: Path) -> Path:
        return self._root / _META_DIR / f"{key}{_META_SUFFIX}"

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with open(temp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def put(
        self,
        *,
        object_key: str,
        body: bytes,
        content_type: str,
        cache_control: str,
    ) -> bool:
        key = _safe_relative_key(object_key)
        if key is None:
            logger.warning("local media put rejected: invalid key=%r", object_key)
            return False

        meta = {
            "content_type": content_type,
            "cache_control": cache_control,
            "created_at": datetime.now(UTC).isoformat(),
            "synced": False,
        }
        try:
            # The object must be durable before the URL is handed to the client;
            # the meta file is written last so a half-written object never looks
            # complete.
            self._write_atomic(self._object_path(key), body)
            self._write_atomic(
                self._meta_path(key),
                json.dumps(meta, separators=(",", ":")).encode("utf-8"),
            )
        except OSError:
            logger.exception("local media put failed: object_key=%s", object_key)
            return False
        return True

    def get(self, object_key: str) -> LocalMediaObject | None:
        key = _safe_relative_key(object_key)
        if key is None:
            return None
        object_path = self._object_path(key)
        try:
            body = object_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            logger.exception("local media read failed: object_key=%s", object_key)
            return None

        content_type = "application/octet-stream"
        cache_control = "public, max-age=31536000, immutable"
        try:
            meta = json.loads(self._meta_path(key).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            meta = {}
        if isinstance(meta, dict):
            content_type = str(meta.get("content_type") or content_type)
            cache_control = str(meta.get("cache_control") or cache_control)
        return LocalMediaObject(
            body=body,
            content_type=content_type,
            cache_control=cache_control,
        )

    def exists(self, object_key: str) -> bool:
        key = _safe_relative_key(object_key)
        return key is not None and self._object_path(key).is_file()

    def pending_keys(self) -> list[str]:
        meta_root = self._root / _META_DIR
        if not meta_root.is_dir():
            return []
        pending: list[str] = []
        for meta_path in meta_root.rglob(f"*{_META_SUFFIX}"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict) or meta.get("synced") is True:
                continue
            relative = meta_path.relative_to(meta_root).as_posix()
            object_key = relative[: -len(_META_SUFFIX)]
            if self.exists(object_key):
                pending.append(object_key)
        return pending

    def mark_synced(self, object_key: str) -> None:
        key = _safe_relative_key(object_key)
        if key is None:
            return
        meta_path = self._meta_path(key)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if not isinstance(meta, dict):
            return
        meta["synced"] = True
        meta["synced_at"] = datetime.now(UTC).isoformat()
        self._write_atomic(
            meta_path,
            json.dumps(meta, separators=(",", ":")).encode("utf-8"),
        )

    def delete(self, object_key: str) -> None:
        key = _safe_relative_key(object_key)
        if key is None:
            return
        self._object_path(key).unlink(missing_ok=True)
        self._meta_path(key).unlink(missing_ok=True)

    def collect_garbage(self, *, retention_hours: float) -> int:
        """Drop synced objects whose local copy outlived its serving window."""

        meta_root = self._root / _META_DIR
        if not meta_root.is_dir():
            return 0
        cutoff = datetime.now(UTC).timestamp() - retention_hours * 3600
        removed = 0
        for meta_path in meta_root.rglob(f"*{_META_SUFFIX}"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict) or meta.get("synced") is not True:
                continue
            synced_at = meta.get("synced_at") or meta.get("created_at")
            if not isinstance(synced_at, str):
                continue
            try:
                synced_timestamp = datetime.fromisoformat(synced_at).timestamp()
            except ValueError:
                continue
            if synced_timestamp > cutoff:
                continue
            relative = meta_path.relative_to(meta_root).as_posix()
            self.delete(relative[: -len(_META_SUFFIX)])
            removed += 1
        return removed


@lru_cache
def get_local_media_store() -> LocalMediaStore:
    return LocalMediaStore(settings.LOCAL_MEDIA_ROOT)
