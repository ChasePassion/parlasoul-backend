from __future__ import annotations

import math
import uuid

import pytest
from pydantic import ValidationError

from src.schemas.realtime_telemetry import (
    RealtimeTelemetryEventInput,
    RealtimeTelemetryEventsBatchRequest,
    RealtimeTelemetrySessionCreateRequest,
    RealtimeTelemetrySessionUpdateRequest,
    RealtimeTurnMetricsWrite,
)


def test_session_create_accepts_only_exact_scenarios_and_safe_build_tokens() -> None:
    request = RealtimeTelemetrySessionCreateRequest(
        mode="chat",
        chat_id=uuid.uuid4(),
        character_id=uuid.uuid4(),
        scenario="network_switch",
        client_build="web-2026.07.17+abc123",
        browser_version="140.0.1",
        os_version="11.0",
    )

    assert request.scenario.value == "network_switch"

    for scenario in ("standard", "network_impairment", "device_switch", "turn_only"):
        with pytest.raises(ValidationError):
            RealtimeTelemetrySessionCreateRequest(
                mode="chat",
                chat_id=uuid.uuid4(),
                character_id=uuid.uuid4(),
                scenario=scenario,
            )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("transcript", "secret"),
        ("assistant_text", "secret"),
        ("error_message", "secret"),
        ("offer_sdp", "secret"),
        ("remote_ip_address", "127.0.0.1"),
        ("device_label", "microphone"),
        ("raw_user_agent", "browser"),
        ("payload", {"latency_ms": 1}),
        ("unknown_metric", 1),
    ],
)
def test_event_payload_rejects_private_nested_or_unknown_fields(key: str, value: object) -> None:
    with pytest.raises(ValidationError):
        RealtimeTelemetryEventInput(
            seq=0,
            event_type="protocol.error",
            stage="client",
            occurred_offset_ms=0,
            payload={key: value},
        )


def test_event_payload_rejects_non_finite_values() -> None:
    for value in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValidationError):
            RealtimeTelemetryEventInput(
                seq=0,
                event_type="protocol.error",
                occurred_offset_ms=0,
                payload={"latency_ms": value},
            )


def test_event_and_error_slugs_must_be_fixed_allowlisted_values() -> None:
    with pytest.raises(ValidationError, match="event_type is not allowlisted"):
        RealtimeTelemetryEventInput(
            seq=0,
            event_type="encoded-private-content",
            occurred_offset_ms=0,
        )
    with pytest.raises(ValidationError, match="reason_code is not allowlisted"):
        RealtimeTelemetryEventInput(
            seq=0,
            event_type="protocol.error",
            reason_code="encoded-private-content",
            occurred_offset_ms=0,
        )
    with pytest.raises(ValidationError, match="error_code is not allowlisted"):
        RealtimeTelemetrySessionUpdateRequest(
            status="failed",
            failure_stage="client",
            error_code="encoded-private-content",
        )




def test_event_batch_is_bounded_to_fifty() -> None:
    event = {
        "seq": 0,
        "event_type": "connect.started",
        "occurred_offset_ms": 0,
    }
    with pytest.raises(ValidationError):
        RealtimeTelemetryEventsBatchRequest(events=[{**event, "seq": seq} for seq in range(51)])


def test_public_patch_rejects_runtime_id_and_unstructured_summary() -> None:
    with pytest.raises(ValidationError):
        RealtimeTelemetrySessionUpdateRequest.model_validate(
            {"status": "connected", "rtc_session_id": "rt_abc"}
        )
    with pytest.raises(ValidationError):
        RealtimeTelemetrySessionUpdateRequest.model_validate(
            {"summary": {"transcript": "secret"}}
        )


def test_generation_id_is_nonnegative_integer() -> None:
    with pytest.raises(ValidationError):
        RealtimeTurnMetricsWrite(turn_seq=0, generation_id=-1)
    with pytest.raises(ValidationError):
        RealtimeTurnMetricsWrite(turn_seq=0, generation_id="free-form")


