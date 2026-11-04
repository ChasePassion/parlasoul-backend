"""Privacy-safe realtime call telemetry API contracts.

These schemas intentionally exclude audio, transcript, SDP, IP addresses, user
agents, device labels, and arbitrary text. Client and server durations must be
derived inside their own monotonic clock domain before being submitted.
"""

from __future__ import annotations

import enum
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.realtime.session.metrics import MetricPayload, sanitize_metric_payload

MAX_BATCH_SIZE = 50
TELEMETRY_SCHEMA_VERSION = 1
STATS_INTERVAL_MS = 1_000

NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Ratio = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class RealtimeTelemetryMode(enum.StrEnum):
    CHAT = "chat"
    LAB = "lab"


class RealtimeTelemetryScenario(enum.StrEnum):
    NORMAL = "normal"
    SLOW_SPEAKER = "slow_speaker"
    BARGE_IN = "barge_in"
    NETWORK_SWITCH = "network_switch"
    MIC_TOGGLE = "mic_toggle"
    BACKGROUND_TAB = "background_tab"


class RealtimeTelemetryStatus(enum.StrEnum):
    STARTING = "starting"
    CONNECTED = "connected"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RealtimeTelemetryFailureStage(enum.StrEnum):
    SIGNALING = "signaling"
    ICE = "ice"
    MEDIA = "media"
    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    PLAYOUT = "playout"
    PERSISTENCE = "persistence"
    CLIENT = "client"
    UNKNOWN = "unknown"


class RealtimeTelemetrySource(enum.StrEnum):
    CLIENT = "client"
    SERVER = "server"


class RealtimeTelemetryEventStage(enum.StrEnum):
    SESSION = "session"
    SIGNALING = "signaling"
    ICE = "ice"
    MEDIA = "media"
    VAD = "vad"
    ENDPOINTING = "endpointing"
    STT = "stt"
    LLM = "llm"
    TTS = "tts"
    PERSISTENCE = "persistence"
    PLAYOUT = "playout"
    SERVER_RTP_SOURCE = "server_rtp_source"
    SERVER_PLAYOUT_QUEUE = "server_playout_queue"
    INTERRUPTION = "interruption"
    STATE_MACHINE = "state_machine"
    CLIENT = "client"
    UNKNOWN = "unknown"


class RealtimeTurnOutcome(enum.StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RealtimeConnectionState(enum.StrEnum):
    NEW = "new"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class RealtimeIceConnectionState(enum.StrEnum):
    NEW = "new"
    CHECKING = "checking"
    CONNECTED = "connected"
    COMPLETED = "completed"
    FAILED = "failed"
    DISCONNECTED = "disconnected"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class RealtimeDataChannelState(enum.StrEnum):
    CONNECTING = "connecting"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class RealtimeCandidateType(enum.StrEnum):
    HOST = "host"
    SERVER_REFLEXIVE = "srflx"
    PEER_REFLEXIVE = "prflx"
    RELAY = "relay"
    UNKNOWN = "unknown"


class RealtimeTransportProtocol(enum.StrEnum):
    UDP = "udp"
    TCP = "tcp"
    TLS = "tls"
    UNKNOWN = "unknown"


class RealtimeAddressFamily(enum.StrEnum):
    IPV4 = "ipv4"
    IPV6 = "ipv6"
    UNKNOWN = "unknown"


class BrowserName(enum.StrEnum):
    CHROME = "chrome"
    EDGE = "edge"
    FIREFOX = "firefox"
    SAFARI = "safari"
    OTHER = "other"
    UNKNOWN = "unknown"


class OperatingSystemName(enum.StrEnum):
    WINDOWS = "windows"
    MACOS = "macos"
    IOS = "ios"
    ANDROID = "android"
    LINUX = "linux"
    CHROMEOS = "chromeos"
    OTHER = "other"
    UNKNOWN = "unknown"


class StrictTelemetryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


ALLOWED_REALTIME_EVENT_TYPES = frozenset(
    {
        "audio.first_audible",
        "audio_stopped",
        "barge_in.candidate",
        "barge_in.candidate_discarded",
        "barge_in.confirmed",
        "barge_in_detected",
        "connect.started",
        "connection.grace_recovered",
        "connection.grace_started",
        "connection.readiness_wait_started",
        "control.queue_flushed",
        "control.queue_full",
        "control.queued",
        "data_channel.closed",
        "data_channel.error",
        "data_channel.open",
        "end_of_turn_confirmed",
        "failure",
        "first_audio_enqueued",
        "first_speakable_chunk",
        "generation.ignored",
        "ice.connection_state",
        "ice.gathering_state",
        "ice.gathering_wait_completed",
        "llm_first_token",
        "llm_request",
        "media.remote_track",
        "media_element.play_failed",
        "media_element.playing",
        "microphone.toggle_failed",
        "microphone.toggled",
        "observation.started",
        "page.visibility",
        "peer_connection.created",
        "peer_connection.state",
        "protocol.error",
        "remote_analyser.resume_failed",
        "remote_analyser.setup_failed",
        "response.audio_stopped",
        "response.done",
        "response.interrupted",
        "response.started",
        "sdp.answer_applied",
        "sdp.local_description_set",
        "server.error",
        "server.session_created",
        "server_first_audio_read",
        "server_playout_drained",
        "session.connected",
        "session.ended",
        "session_started",
        "signaling.state",
        "speech.started",
        "speech.stopped",
        "speech_start",
        "speech_stop",
        "state_transition",
        "stats.sample_failed",
        "stt_final",
        "stt_start",
        "subtitle.delta",
        "subtitle.final",
        "tts_first_audio",
        "tts_input_closed",
        "tts_request_start",
        "tts_start",
        "utterance_discarded",
    }
)

ALLOWED_REALTIME_REASON_CODES = frozenset(
    {
        "analyser_unavailable",
        "audio_context_suspended",
        "audio_stopped",
        "audio_track_missing",
        "cancelled",
        "client_response_cancelled",
        "connect_failed",
        "connection_failed",
        "control_queue_full",
        "data_channel_closed",
        "data_channel_error",
        "data_channel_readiness_timeout",
        "disconnected_timeout",
        "disconnect_grace_expired",
        "empty",
        "end_of_turn_confirmed",
        "first_audio_enqueued",
        "forbidden",
        "get_stats_failed",
        "ice_disconnected",
        "ice_failed",
        "ice_readiness_timeout",
        "internal_error",
        "interrupt_not_confirmed",
        "invalid_event_payload",
        "invalid_param",
        "invalid_request",
        "invalid_subtitle_channel",
        "llm_failure",
        "llm_service_error",
        "manual_mute",
        "mic_muted",
        "negotiation_failed",
        "negotiation_timeout",
        "peer_connection_disconnected",
        "peer_connection_failed",
        "play_rejected",
        "playout_drained",
        "playout_failure",
        "persistence_failure",
        "realtime_error",
        "response_cancelled_before_playout",
        "response_completed",
        "response_done",
        "response_failed",
        "response_started",
        "server_shutdown",
        "session_closed",
        "session_conflict",
        "session_start_failed",
        "signaling_readiness_timeout",
        "soft_interrupt_resumed",
        "soft_resume",
        "speech_stopped_before_confirm",
        "stale_generation",
        "stt_empty",
        "stt_failure",
        "stt_upstream_error",
        "timeout",
        "too_short",
        "tts_failure",
        "tts_upstream_error",
        "unknown_error",
        "unknown_event_type",
        "user_speech_detected",
        "user_speech_while_thinking",
        "user_utterance_committed",
        "vad_silence",
        "vad_speech_started",
        "vad_speech_stopped",
    }
)

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_BUILD_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._+-]{0,78}[A-Za-z0-9])?$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._+-]{0,30}[A-Za-z0-9])?$")
_RTC_SESSION_RE = re.compile(r"^rt_[a-z0-9]+$")

_NUMERIC_EVENT_METRICS = frozenset(
    {
        "duration_ms",
        "elapsed_ms",
        "latency_ms",
        "offset_ms",
        "queue_depth",
        "queue_depth_ms",
        "buffered_ms",
        "buffered_amount",
        "queue_size",
        "frame_count",
        "sample_count",
        "segment_count",
        "retry_count",
        "chunk_count",
        "turn_seq",
        "generation_id",
        "pacer_underrun_count",
        "input_char_count",
        "input_word_count",
        "output_char_count",
        "output_word_count",
        "char_count",
        "audio_level",
        "timeout_ms",
        "audio_duration_ms",
        "ttfa_ms",
        "playout_latency_ms",
        "barge_in_stop_latency_ms",
        "confidence",
        "voiced_ms",
        "utterance_ms",
        "recognized_char_count",
        "speakable_char_count",
        "pacer_relative_ms",
        "drained_ms",
        "peak_buffered_ms",
    }
)
_BOOLEAN_EVENT_METRICS = frozenset(
    {
        "translation_enabled",
        "ice_gathering_timed_out",
        "muted",
        "mic_enabled",
        "ready",
        "counter_reset",
        "is_empty",
        "is_browser_playback",
        "fade_consumed",
        "stt_streaming",
    }
)
_ENUM_EVENT_METRICS = {
    "state": frozenset(
        {
            "starting",
            "connected",
            "completed",
            "cancelled",
            "new",
            "connecting",
            "checking",
            "disconnected",
            "failed",
            "closed",
            "unknown",
            "listening",
            "user_speaking",
            "endpoint_pending",
            "thinking",
            "agent_speaking",
            "interrupting",
            "gathering",
            "complete",
            "stable",
            "have-local-offer",
            "have-remote-offer",
            "have-local-pranswer",
            "have-remote-pranswer",
            "received",
            "open",
            "closing",
            "playing",
            "hidden",
            "visible",
        }
    ),
    "previous_state": frozenset(
        {
            "starting",
            "connected",
            "completed",
            "cancelled",
            "new",
            "connecting",
            "checking",
            "disconnected",
            "failed",
            "closed",
            "unknown",
            "listening",
            "user_speaking",
            "endpoint_pending",
            "thinking",
            "agent_speaking",
            "interrupting",
            "gathering",
            "complete",
            "stable",
            "have-local-offer",
            "have-remote-offer",
            "have-local-pranswer",
            "have-remote-pranswer",
            "received",
            "open",
            "closing",
            "playing",
            "hidden",
            "visible",
        }
    ),
    "channel": frozenset({"user_raw", "assistant_raw", "assistant_translated"}),
    "source": frozenset(
        {
            "client",
            "server",
            "webrtc",
            "vad",
            "stt",
            "llm",
            "tts",
            "playout",
            "pacer",
            "manager",
            "remote",
            "sdp_answer_applied",
        }
    ),
    "connection_state": frozenset(
        {"new", "connecting", "connected", "disconnected", "failed", "closed", "unknown"}
    ),
    "ice_connection_state": frozenset(
        {
            "new",
            "checking",
            "connected",
            "completed",
            "disconnected",
            "failed",
            "closed",
            "unknown",
        }
    ),
    "data_channel_state": frozenset({"connecting", "open", "closing", "closed", "unknown"}),
    "candidate_type": frozenset({"host", "srflx", "prflx", "relay", "unknown"}),
    "protocol": frozenset({"udp", "tcp", "tls", "unknown"}),
    "address_family": frozenset({"ipv4", "ipv6", "unknown"}),
    "outcome": frozenset({"completed", "interrupted", "failed", "cancelled"}),
}


def _validate_slug(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not _SLUG_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase slug")
    return normalized


def _validate_allowlisted_slug(
    value: str | None,
    *,
    field_name: str,
    allowed: frozenset[str],
) -> str | None:
    normalized = _validate_slug(value, field_name=field_name)
    if normalized is not None and normalized not in allowed:
        raise ValueError(f"{field_name} is not allowlisted")
    return normalized


def _validate_safe_token(
    value: str | None,
    *,
    field_name: str,
    pattern: re.Pattern[str],
) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not pattern.fullmatch(normalized):
        raise ValueError(f"{field_name} has invalid format")
    return normalized


def validate_rtc_session_id(value: str) -> str:
    normalized = value.strip().lower()
    if not _RTC_SESSION_RE.fullmatch(normalized):
        raise ValueError("rtc_session_id has invalid format")
    return normalized


def sanitize_event_payload(
    metrics: MetricPayload | None,
) -> dict[str, int | float | bool | str]:
    """Reject unknown keys and any non-scalar or privacy-unsafe metric value."""

    sanitized = sanitize_metric_payload(metrics)
    result: dict[str, int | float | bool | str] = {}
    for key, value in sanitized.items():
        if key in _NUMERIC_EVENT_METRICS:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"realtime metric {key} must be non-negative numeric")
        elif key in _BOOLEAN_EVENT_METRICS:
            if not isinstance(value, bool):
                raise ValueError(f"realtime metric {key} must be boolean")
        elif key in _ENUM_EVENT_METRICS:
            if not isinstance(value, str) or value not in _ENUM_EVENT_METRICS[key]:
                raise ValueError(f"realtime metric {key} has invalid enum value")
        else:
            raise ValueError(f"realtime metric {key} is not allowlisted")
        result[key] = value
    return result


class RealtimeSafeConfig(StrictTelemetryModel):
    """Allowlisted, non-secret knobs useful when comparing test runs."""

    stats_interval_ms: Annotated[int, Field(ge=500, le=10_000)] = STATS_INTERVAL_MS
    ice_transport_policy: Literal["all", "relay"] | None = None
    vad_start_ms: Annotated[int, Field(ge=0, le=10_000)] | None = None
    vad_stop_ms: Annotated[int, Field(ge=0, le=30_000)] | None = None
    endpoint_delay_ms: Annotated[int, Field(ge=0, le=30_000)] | None = None
    interruption_min_ms: Annotated[int, Field(ge=0, le=10_000)] | None = None
    playout_target_ms: Annotated[int, Field(ge=0, le=10_000)] | None = None
    playout_max_ms: Annotated[int, Field(ge=0, le=30_000)] | None = None
    audio_frame_ms: Literal[10, 20, 40, 60] | None = None


class RealtimeSessionSummary(StrictTelemetryModel):
    """Safe single-session aggregate returned directly to the test UI."""

    ttfa_ms: NonNegativeFloat | None = None
    barge_in_stop_ms: NonNegativeFloat | None = None
    playout_queue_peak_ms: NonNegativeFloat | None = None
    avg_rtt_ms: NonNegativeFloat | None = None
    packet_loss_ratio: Ratio | None = None
    avg_jitter_buffer_ms: NonNegativeFloat | None = None
    concealed_ratio: Ratio | None = None
    stats_sample_count: NonNegativeInt = 0
    failed_turn_count: NonNegativeInt = 0
    turn_count: NonNegativeInt = 0
    interruption_count: NonNegativeInt = 0
    dropped_event_count: NonNegativeInt = 0


class RealtimeTelemetrySessionCreateRequest(StrictTelemetryModel):
    chat_id: uuid.UUID | None = None
    character_id: uuid.UUID | None = None
    mode: RealtimeTelemetryMode = RealtimeTelemetryMode.CHAT
    scenario: RealtimeTelemetryScenario = RealtimeTelemetryScenario.NORMAL
    experiment_tag: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    client_build: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    browser_name: BrowserName = BrowserName.UNKNOWN
    browser_version: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    os_name: OperatingSystemName = OperatingSystemName.UNKNOWN
    os_version: Annotated[str, Field(min_length=1, max_length=32)] | None = None
    safe_config: RealtimeSafeConfig = Field(default_factory=RealtimeSafeConfig)

    @field_validator("experiment_tag")
    @classmethod
    def normalize_experiment_tag(cls, value: str | None) -> str | None:
        return _validate_slug(value, field_name="experiment_tag")

    @field_validator("client_build")
    @classmethod
    def validate_client_build(cls, value: str | None) -> str | None:
        return _validate_safe_token(value, field_name="client_build", pattern=_BUILD_RE)

    @field_validator("browser_version", "os_version")
    @classmethod
    def validate_versions(cls, value: str | None, info: Any) -> str | None:
        return _validate_safe_token(value, field_name=info.field_name, pattern=_VERSION_RE)

    @model_validator(mode="after")
    def validate_context(self) -> RealtimeTelemetrySessionCreateRequest:
        if (self.chat_id is None) != (self.character_id is None):
            raise ValueError("chat_id and character_id must be provided together")
        if self.mode == RealtimeTelemetryMode.CHAT and self.chat_id is None:
            raise ValueError("chat mode requires chat_id and character_id")
        if self.mode == RealtimeTelemetryMode.LAB and self.chat_id is not None:
            raise ValueError("lab mode cannot reference chat context")
        return self


class RealtimeTelemetrySessionCreateResponse(StrictTelemetryModel):
    observation_id: uuid.UUID
    schema_version: Literal[1] = TELEMETRY_SCHEMA_VERSION
    stats_interval_ms: Literal[1000] = STATS_INTERVAL_MS
    max_batch_size: Literal[50] = MAX_BATCH_SIZE


class RealtimeTelemetryEventInput(StrictTelemetryModel):
    seq: NonNegativeInt
    event_type: Annotated[str, Field(min_length=1, max_length=80)]
    stage: RealtimeTelemetryEventStage | None = None
    reason_code: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    occurred_offset_ms: NonNegativeInt
    payload: dict[str, int | float | bool | str] = Field(default_factory=dict)

    @field_validator("event_type")
    @classmethod
    def normalize_event_type(cls, value: str) -> str:
        normalized = _validate_allowlisted_slug(
            value,
            field_name="event_type",
            allowed=ALLOWED_REALTIME_EVENT_TYPES,
        )
        assert normalized is not None
        return normalized

    @field_validator("reason_code")
    @classmethod
    def normalize_reason_code(cls, value: str | None) -> str | None:
        return _validate_allowlisted_slug(
            value,
            field_name="reason_code",
            allowed=ALLOWED_REALTIME_REASON_CODES,
        )

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> dict[str, int | float | bool | str]:
        if not isinstance(value, dict):
            raise ValueError("payload must be an object")
        try:
            return sanitize_event_payload(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


class RealtimeTelemetryEventsBatchRequest(StrictTelemetryModel):
    events: Annotated[list[RealtimeTelemetryEventInput], Field(min_length=1, max_length=50)]


class RealtimeWebRTCStatsInput(StrictTelemetryModel):
    sample_seq: NonNegativeInt
    sampled_offset_ms: NonNegativeInt
    connection_state: RealtimeConnectionState
    ice_connection_state: RealtimeIceConnectionState
    data_channel_state: RealtimeDataChannelState | None = None
    local_candidate_type: RealtimeCandidateType | None = None
    remote_candidate_type: RealtimeCandidateType | None = None
    candidate_protocol: RealtimeTransportProtocol | None = None
    address_family: RealtimeAddressFamily | None = None
    rtt_ms: Annotated[float, Field(ge=0, le=300_000, allow_inf_nan=False)] | None = None
    inbound_jitter_ms: Annotated[float, Field(ge=0, le=300_000, allow_inf_nan=False)] | None = None
    packet_loss_ratio: Ratio | None = None
    remote_inbound_fraction_lost: Ratio | None = None
    remote_inbound_jitter_ms: Annotated[
        float, Field(ge=0, le=300_000, allow_inf_nan=False)
    ] | None = None
    remote_inbound_rtt_ms: Annotated[
        float, Field(ge=0, le=300_000, allow_inf_nan=False)
    ] | None = None
    remote_inbound_packets_lost_delta: NonNegativeInt | None = None
    inbound_packets_received_delta: NonNegativeInt | None = None
    inbound_packets_lost_delta: NonNegativeInt | None = None
    inbound_bytes_received_delta: NonNegativeInt | None = None
    outbound_packets_sent_delta: NonNegativeInt | None = None
    outbound_bytes_sent_delta: NonNegativeInt | None = None
    jitter_buffer_delay_ms: Annotated[
        float, Field(ge=0, le=300_000, allow_inf_nan=False)
    ] | None = None
    concealed_ratio: Ratio | None = None
    concealed_samples_delta: NonNegativeInt | None = None
    silent_concealed_samples_delta: NonNegativeInt | None = None
    audio_level: Ratio | None = None
    codec_mime_type: Literal["audio/opus", "audio/pcmu", "audio/pcma"] | None = None
    codec_clock_rate: Annotated[int, Field(ge=8_000, le=192_000)] | None = None
    control_queue_size: NonNegativeInt | None = None
    data_channel_buffered_amount: NonNegativeInt | None = None
    counter_reset: bool = False
    available_outgoing_bitrate_bps: NonNegativeFloat | None = None


class RealtimeWebRTCStatsBatchRequest(StrictTelemetryModel):
    samples: Annotated[list[RealtimeWebRTCStatsInput], Field(min_length=1, max_length=50)]


class RealtimeTelemetryBatchResponse(StrictTelemetryModel):
    accepted_count: NonNegativeInt
    duplicate_count: NonNegativeInt


class RealtimeTelemetrySessionUpdateRequest(StrictTelemetryModel):
    status: RealtimeTelemetryStatus | None = None
    failure_stage: RealtimeTelemetryFailureStage | None = None
    error_code: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    duration_ms: NonNegativeInt | None = None
    turn_count: NonNegativeInt | None = None
    interruption_count: NonNegativeInt | None = None
    summary: RealtimeSessionSummary | None = None

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        return _validate_allowlisted_slug(
            value,
            field_name="error_code",
            allowed=ALLOWED_REALTIME_REASON_CODES,
        )

    @model_validator(mode="after")
    def validate_update(self) -> RealtimeTelemetrySessionUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        if self.status == RealtimeTelemetryStatus.FAILED and self.failure_stage is None:
            raise ValueError("failed status requires failure_stage")
        if self.failure_stage is not None and self.status not in {
            None,
            RealtimeTelemetryStatus.FAILED,
        }:
            raise ValueError("failure_stage is only valid for failed status")
        return self


class RealtimeTurnMetricsWrite(StrictTelemetryModel):
    turn_seq: NonNegativeInt
    generation_id: NonNegativeInt
    outcome: RealtimeTurnOutcome = RealtimeTurnOutcome.COMPLETED
    failure_stage: RealtimeTelemetryFailureStage | None = None
    error_code: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    speech_duration_ms: NonNegativeInt | None = None
    endpointing_latency_ms: NonNegativeInt | None = None
    stt_first_partial_latency_ms: NonNegativeInt | None = None
    stt_final_latency_ms: NonNegativeInt | None = None
    llm_ttft_ms: NonNegativeInt | None = None
    first_speakable_chunk_latency_ms: NonNegativeInt | None = None
    tts_ttfb_ms: NonNegativeInt | None = None
    first_audio_enqueued_latency_ms: NonNegativeInt | None = None
    server_first_audio_read_latency_ms: NonNegativeInt | None = None
    client_first_audio_played_latency_ms: NonNegativeInt | None = None
    ttfa_ms: NonNegativeInt | None = None
    barge_in_stop_latency_ms: NonNegativeInt | None = None
    playout_queue_peak_ms: NonNegativeInt | None = None
    playout_queue_final_ms: NonNegativeInt | None = None
    server_playout_drained_latency_ms: NonNegativeInt | None = None
    pacer_underrun_count: NonNegativeInt = 0
    pacer_partial_pad_count: NonNegativeInt = 0
    pacer_frames_read: NonNegativeInt = 0
    pacer_silence_frames: NonNegativeInt = 0
    pacer_rejected_write_count: NonNegativeInt = 0
    pacer_fadeout_count: NonNegativeInt = 0
    input_char_count: NonNegativeInt = 0
    input_word_count: NonNegativeInt = 0
    output_char_count: NonNegativeInt = 0
    output_word_count: NonNegativeInt = 0

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        return _validate_allowlisted_slug(
            value,
            field_name="error_code",
            allowed=ALLOWED_REALTIME_REASON_CODES,
        )


class RealtimeTelemetryEventResponse(StrictTelemetryModel):
    id: int
    source: RealtimeTelemetrySource
    seq: int
    event_type: str
    stage: RealtimeTelemetryEventStage | None
    reason_code: str | None
    occurred_offset_ms: int
    received_at: datetime
    payload: dict[str, int | float | bool | str]

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: Any) -> dict[str, int | float | bool | str]:
        if not isinstance(value, dict):
            raise ValueError("payload must be an object")
        try:
            return sanitize_event_payload(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


class RealtimeTurnMetricsResponse(RealtimeTurnMetricsWrite):
    id: int
    created_at: datetime
    updated_at: datetime


class RealtimeWebRTCStatsResponse(RealtimeWebRTCStatsInput):
    id: int
    received_at: datetime


class RealtimeTelemetrySessionResponse(StrictTelemetryModel):
    observation_id: uuid.UUID
    chat_id: uuid.UUID | None
    character_id: uuid.UUID | None
    rtc_session_id: str | None
    mode: RealtimeTelemetryMode
    scenario: RealtimeTelemetryScenario
    experiment_tag: str | None
    client_build: str | None
    server_build: str | None
    browser_name: BrowserName
    browser_version: str | None
    os_name: OperatingSystemName
    os_version: str | None
    status: RealtimeTelemetryStatus
    failure_stage: RealtimeTelemetryFailureStage | None
    error_code: str | None
    started_at: datetime
    connected_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None
    turn_count: int
    interruption_count: int
    safe_config: RealtimeSafeConfig
    summary: RealtimeSessionSummary
    created_at: datetime
    updated_at: datetime


class RealtimeTelemetrySessionPageResponse(StrictTelemetryModel):
    items: list[RealtimeTelemetrySessionResponse]
    next_cursor: str | None = None


class RealtimeTelemetrySessionDetailResponse(StrictTelemetryModel):
    session: RealtimeTelemetrySessionResponse
    events: list[RealtimeTelemetryEventResponse]
    turns: list[RealtimeTurnMetricsResponse]
    stats_samples: list[RealtimeWebRTCStatsResponse]


__all__ = [
    "ALLOWED_REALTIME_EVENT_TYPES",
    "ALLOWED_REALTIME_REASON_CODES",
    "MAX_BATCH_SIZE",
    "STATS_INTERVAL_MS",
    "TELEMETRY_SCHEMA_VERSION",
    "RealtimeSessionSummary",
    "RealtimeTelemetryBatchResponse",
    "RealtimeTelemetryEventInput",
    "RealtimeTelemetryEventsBatchRequest",
    "RealtimeTelemetrySessionCreateRequest",
    "RealtimeTelemetrySessionCreateResponse",
    "RealtimeTelemetrySessionDetailResponse",
    "RealtimeTelemetrySessionPageResponse",
    "RealtimeTelemetrySessionResponse",
    "RealtimeTelemetrySessionUpdateRequest",
    "RealtimeTurnMetricsWrite",
    "RealtimeWebRTCStatsBatchRequest",
]
