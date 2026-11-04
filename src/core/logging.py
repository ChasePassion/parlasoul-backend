"""Centralized logging setup with human-friendly formatting."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from pathlib import Path
from queue import Queue
from typing import Any

from src.core.config import settings
from src.core.logging_context import current_log_context

_LOG_LISTENER: QueueListener | None = None
_LOGGING_CONFIGURED = False

_SUMMARY_FIELDS = (
    "event",
    "request_id",
    "llm_call_id",
    "voice_call_id",
    "operation",
    "route_role",
    "provider",
    "model",
    "sdk_kind",
    "provider_voice_id",
    "audio_format",
    "user_id",
    "chat_id",
    "turn_id",
    "candidate_id",
    "character_id",
    "elapsed_ms",
    "finalize_ms",
    "upstream_status",
    "upstream_code",
    "source",
    "segment_idx",
    "segment_chars",
    "segment_text_preview",
    "tail_segment_count",
    "tail_chars",
    "queued_segments",
    "queued_segments_before_tail",
    "accumulated_chars",
    "buffer_chars",
    "first_segment_queued",
    "first_segment_to_audio_ms",
    "connect_ms",
    "task_start_ms",
    "setup_elapsed_ms",
    "first_segment_send_elapsed_ms",
    "warm_key",
    "warm_age_ms",
    "warm_wait_ms",
    "warm_status",
    "warm_keepalive_ms",
    "warm_refresh_in_ms",
    "ttl_ms",
    "cumulative_chars",
    "frame_bytes",
    "frame_ms",
    "is_final",
)


class ContextFilter(logging.Filter):
    """Inject request and llm contextvars into each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = current_log_context()
        for key, value in context.items():
            if not getattr(record, key, None):
                setattr(record, key, value)
        return True


class HumanFriendlyFormatter(logging.Formatter):
    """Render application logs as readable single-line summaries."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        timestamp = datetime.fromtimestamp(record.created, tz=UTC)
        return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        module = getattr(record, "log_module", None) or record.name.split(".")[-1]
        parts = [
            f"[{self.formatTime(record)}]",
            f"[{record.levelname}]",
            f"[{module}]",
            message,
        ]
        for field in _SUMMARY_FIELDS:
            value = getattr(record, field, None)
            if value is None or value == "":
                continue
            parts.append(f"[{field}={value}]")

        if record.exc_info:
            parts.append(self.formatException(record.exc_info))
        return " ".join(parts)


def _build_handlers() -> tuple[list[logging.Handler], QueueListener | None]:
    formatter = HumanFriendlyFormatter()
    context_filter = ContextFilter()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(settings.APP_LOG_LEVEL)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)

    log_dir = Path(settings.APP_LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        log_dir / "app.log",
        when="midnight",
        backupCount=settings.APP_LOG_BACKUP_COUNT,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setLevel(settings.APP_LOG_LEVEL)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)

    if not settings.APP_LOG_QUEUE_ENABLED:
        return [console_handler, file_handler], None

    queue: Queue[Any] = Queue(-1)
    listener = QueueListener(queue, console_handler, file_handler, respect_handler_level=True)
    queue_handler = QueueHandler(queue)
    queue_handler.setLevel(settings.APP_LOG_LEVEL)
    return [queue_handler], listener


def setup_logging() -> None:
    """Configure root logging exactly once."""

    global _LOG_LISTENER, _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    handlers, listener = _build_handlers()
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.APP_LOG_LEVEL)
    for handler in handlers:
        root.addHandler(handler)

    _LOG_LISTENER = listener
    if _LOG_LISTENER is not None:
        _LOG_LISTENER.start()

    _LOGGING_CONFIGURED = True


def shutdown_logging() -> None:
    """Stop the queue listener on shutdown."""

    global _LOG_LISTENER
    if _LOG_LISTENER is not None:
        _LOG_LISTENER.stop()
        _LOG_LISTENER = None
