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
    RealtimeWebRTCStatsInput,
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


def test_runtime_event_stages_and_payloads_are_accepted() -> None:
    cases = [
        ("session_started", "session", {}),
        ("speech_start", "vad", {"confidence": 0.9, "offset_ms": 20}),
        (
            "end_of_turn_confirmed",
            "endpointing",
            {"utterance_ms": 800, "voiced_ms": 620, "offset_ms": 1000},
        ),
        ("stt_final", "stt", {"latency_ms": 210, "recognized_char_count": 12, "is_empty": False}),
        ("llm_first_token", "llm", {"latency_ms": 180, "offset_ms": 1400}),
        ("tts_first_audio", "tts", {"pacer_relative_ms": 90, "offset_ms": 1550}),
        (
            "server_playout_drained",
            "server_rtp_source",
            {"drained_ms": 1200, "peak_buffered_ms": 320},
        ),
        (
            "first_audio_enqueued",
            "server_playout_queue",
            {"buffered_ms": 80, "pacer_relative_ms": 90},
        ),
        ("barge_in_detected", "interruption", {"offset_ms": 1800}),
        (
            "state_transition",
            "state_machine",
            {"previous_state": "thinking", "state": "agent_speaking", "offset_ms": 1600},
        ),
        ("failure", "persistence", {"duration_ms": 10}),
        ("response.done", "playout", {"ready": True}),
    ]

    for seq, (event_type, stage, payload) in enumerate(cases):
        event = RealtimeTelemetryEventInput(
            seq=seq,
            event_type=event_type,
            stage=stage,
            occurred_offset_ms=seq,
            payload=payload,
        )
        assert event.stage is not None


def test_client_event_values_used_by_webrtc_collector_are_accepted() -> None:
    events = [
        ("signaling.state", {"state": "have-local-offer"}),
        ("ice.gathering_state", {"state": "gathering", "elapsed_ms": 10}),
        ("media.remote_track", {"state": "received", "source": "remote"}),
        ("sdp.answer_applied", {"source": "sdp_answer_applied", "ready": True}),
        ("data_channel.open", {"state": "open", "queue_size": 0}),
        ("page.visibility", {"state": "hidden"}),
        ("microphone.toggled", {"mic_enabled": False}),
        ("subtitle.final", {"channel": "assistant_translated", "char_count": 8}),
        (
            "audio.first_audible",
            {
                "generation_id": 2,
                "ttfa_ms": 820,
                "playout_latency_ms": 190,
                "audio_level": 0.2,
            },
        ),
    ]
    for seq, (event_type, payload) in enumerate(events):
        RealtimeTelemetryEventInput(
            seq=seq,
            event_type=event_type,
            occurred_offset_ms=seq,
            payload=payload,
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


def test_webrtc_stats_accepts_typed_upstream_and_downstream_metrics() -> None:
    sample = RealtimeWebRTCStatsInput(
        sample_seq=1,
        sampled_offset_ms=1000,
        connection_state="connected",
        ice_connection_state="completed",
        rtt_ms=50,
        inbound_jitter_ms=4,
        packet_loss_ratio=0.01,
        remote_inbound_fraction_lost=0.02,
        remote_inbound_jitter_ms=3,
        remote_inbound_rtt_ms=45,
        remote_inbound_packets_lost_delta=1,
        local_candidate_type="relay",
        remote_candidate_type="srflx",
        candidate_protocol="udp",
        address_family="ipv4",
        codec_mime_type="audio/opus",
        codec_clock_rate=48000,
    )

    assert sample.remote_inbound_fraction_lost == pytest.approx(0.02)
