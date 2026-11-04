"""Canonical API error code constants."""

from __future__ import annotations


class ErrorCode:
    INVALID_PARAM = "invalid_param"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    RESOURCE_NOT_FOUND = "resource_not_found"
    RESOURCE_CONFLICT = "resource_conflict"
    UPLOAD_FILE_TOO_LARGE = "upload_file_too_large"
    UPLOAD_FILE_TYPE_NOT_ALLOWED = "upload_file_type_not_allowed"
    VALIDATION_FAILED = "validation_failed"
    TOO_MANY_REQUESTS = "too_many_requests"
    INTERNAL_ERROR = "internal_error"
    EXTERNAL_ERROR = "external_error"
    DB_CONNECTION_ERROR = "db_connection_error"
    MEMORY_UNAVAILABLE = "memory_unavailable"
    EXTERNAL_TIMEOUT = "external_timeout"
    LLM_SERVICE_ERROR = "llm_service_error"
    STT_UPSTREAM_ERROR = "stt_upstream_error"
    STT_NO_SPEECH_DETECTED = "stt_no_speech_detected"
    TTS_UPSTREAM_ERROR = "tts_upstream_error"
    TTS_CANDIDATE_FORBIDDEN = "tts_candidate_forbidden"
    TTS_CANDIDATE_INVALID_AUTHOR = "tts_candidate_invalid_author"


__all__ = ["ErrorCode"]
