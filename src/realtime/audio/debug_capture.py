"""Disabled legacy realtime audio capture compatibility shim.

Plan A forbids retaining raw audio or full transcripts. The previous debug
capture wrote WAV files and complete TTS segments, so it is intentionally
disabled at the code boundary. Numeric diagnostics now flow through the
``RealtimeSessionMetricsSink`` contract instead.
"""

from __future__ import annotations

from typing import Any, Self


class RealtimeAudioDebugCapture:
    """No-op shim kept only to avoid breaking older gateway call signatures."""

    @classmethod
    def create_if_enabled(
        cls,
        *,
        enabled: bool,
        base_dir: str,
        session_id: str,
        chat_id: str,
        character_id: str,
        pacer_sample_rate: int,
        max_bytes_per_file: int,
    ) -> Self | None:
        del (
            cls,
            enabled,
            base_dir,
            session_id,
            chat_id,
            character_id,
            pacer_sample_rate,
            max_bytes_per_file,
        )
        return None

    def record_tts_segment(self, *, index: int, text: str) -> None:
        del index, text

    def record_tts_event(
        self,
        *,
        event_type: str,
        audio_delta_chars: int | None = None,
    ) -> None:
        del event_type, audio_delta_chars

    def record_event(self, event: str, fields: dict[str, Any] | None = None) -> None:
        del event, fields

    def write_tts_raw(self, pcm16: bytes, *, sample_rate: int) -> None:
        del pcm16, sample_rate

    def write_tts_resampled(self, pcm16: bytes, *, sample_rate: int) -> None:
        del pcm16, sample_rate

    def write_pacer_output(self, pcm16: bytes, *, sample_rate: int) -> None:
        del pcm16, sample_rate

    def finish(self, outcome: str) -> None:
        del outcome
