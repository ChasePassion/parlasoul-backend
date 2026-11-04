"""Privacy-safe metrics contract for realtime voice sessions.

The contract deliberately accepts only scalar measurements and bounded enum
labels. Audio, transcripts, SDP, IP addresses, and arbitrary nested payloads
cannot pass validation and must never reach a persistence sink.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from enum import Enum
from typing import Protocol, TypeAlias, runtime_checkable

MetricValue: TypeAlias = int | float | bool | str
MetricPayload: TypeAlias = Mapping[str, MetricValue]

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_FORBIDDEN_KEY_PARTS = frozenset(
    {
        "pcm",
        "wav",
        "transcript",
        "text",
        "content",
        "prompt",
        "sdp",
        "ip",
        "address",
    }
)


@runtime_checkable
class RealtimeSessionMetricsSink(Protocol):
    async def record_event(
        self,
        *,
        session_id: str,
        event_type: str,
        generation_id: int | None = None,
        stage: str | None = None,
        reason_code: str | None = None,
        metrics: MetricPayload | None = None,
    ) -> None: ...

    async def upsert_turn_metrics(
        self,
        *,
        session_id: str,
        generation_id: int,
        turn_id: uuid.UUID | None = None,
        metrics: MetricPayload,
    ) -> None: ...


def validate_metric_name(value: str, *, field_name: str) -> str:
    normalized = value.strip().lower()
    if not _SAFE_NAME.fullmatch(normalized):
        raise ValueError(f"invalid realtime metric {field_name}")
    return normalized


def sanitize_metric_payload(metrics: MetricPayload | None) -> dict[str, MetricValue]:
    """Return a normalized scalar payload or reject privacy-unsafe fields."""

    if not metrics:
        return {}
    sanitized: dict[str, MetricValue] = {}
    for raw_key, raw_value in metrics.items():
        key = validate_metric_name(str(raw_key), field_name="key")
        key_parts = set(key.replace("-", ".").replace("_", ".").split("."))
        contains_raw_audio = "audio" in key_parts and bool(
            key_parts & {"raw", "bytes", "data", "payload", "chunk", "samples"}
        )
        if key_parts & _FORBIDDEN_KEY_PARTS or contains_raw_audio:
            raise ValueError("privacy-unsafe realtime metric key")

        value: MetricValue
        if isinstance(raw_value, bool):
            value = raw_value
        elif isinstance(raw_value, int):
            value = raw_value
        elif isinstance(raw_value, float):
            if not math.isfinite(raw_value):
                raise ValueError("realtime metric values must be finite")
            value = raw_value
        elif isinstance(raw_value, Enum):
            value = validate_metric_name(str(raw_value.value), field_name="enum")
        elif isinstance(raw_value, str):
            value = validate_metric_name(raw_value, field_name="enum")
        else:
            raise TypeError("realtime metrics support only numbers, booleans, and enums")
        sanitized[key] = value
    return sanitized


def sanitize_metric_context(
    *,
    event_type: str,
    stage: str | None,
    reason_code: str | None,
) -> tuple[str, str | None, str | None]:
    return (
        validate_metric_name(event_type, field_name="event_type"),
        validate_metric_name(stage, field_name="stage") if stage is not None else None,
        validate_metric_name(reason_code, field_name="reason_code")
        if reason_code is not None
        else None,
    )
