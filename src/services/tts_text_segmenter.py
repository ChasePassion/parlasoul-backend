"""Conversation TTS text segmentation for streamed chat responses."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

STRONG_BOUNDARIES = {"。", "！", "？", ".", "!", "?", "\n"}
MEDIUM_BOUNDARIES = {"；", ";", "：", ":"}
WEAK_BOUNDARIES = {"，", ",", "、"}

_NATURAL_BOUNDARIES = MEDIUM_BOUNDARIES | WEAK_BOUNDARIES
_MARKDOWN_EMPHASIS_RE = re.compile(r"(?<!\w)([*_]{1,3})([^*_]+?)\1(?!\w)")
_COMMON_ABBREVIATIONS = {
    "mr",
    "mrs",
    "ms",
    "dr",
    "prof",
    "sr",
    "jr",
    "vs",
    "etc",
    "e.g",
    "i.e",
    "a.m",
    "p.m",
    "u.s",
    "u.k",
}
_PREFIX_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "sr", "jr"}


@dataclass(frozen=True, slots=True)
class ConversationTTSSegmenterConfig:
    first_min_chars: int = 40
    first_target_chars: int = 56
    first_deadline_ms: int = 700
    first_hard_max: int = 75
    medium_min_chars: int = 55
    weak_min_chars: int = 65
    target_chars: int = 130
    soft_max_chars: int = 160
    hard_max_chars: int = 200


class ConversationTTSSegmenter:
    """Build MiniMax-friendly TTS text segments from streamed LLM chunks.

    This segmenter is intentionally separate from realtime voice-call
    segmentation. Chat SSE TTS optimizes naturalness after the first packet,
    while realtime voice calls keep their own latency-tuned strategy.
    """

    def __init__(
        self,
        config: ConversationTTSSegmenterConfig | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or ConversationTTSSegmenterConfig()
        self._buffer = ""
        self._monotonic = monotonic
        self._segment_started_at: float | None = None
        self._segments_emitted = 0

    @property
    def buffer(self) -> str:
        return self._buffer

    def feed(self, text: str) -> list[str]:
        if text:
            if not self._buffer.strip():
                self._segment_started_at = self._monotonic()
            self._buffer += text
        return self._drain_ready_segments(final=False)

    def flush(self) -> list[str]:
        return self._drain_ready_segments(final=True)

    def _drain_ready_segments(self, *, final: bool) -> list[str]:
        segments: list[str] = []
        while self._buffer.strip():
            break_index = self._find_break_index(final=final)
            if break_index <= 0:
                break
            segment = self._consume_segment(break_index)
            if segment:
                segments.append(segment)
        return segments

    def _find_break_index(self, *, final: bool) -> int:
        text = self._buffer
        if not text.strip():
            return 0

        if strong_break := self._find_strong_break(text):
            return strong_break

        if self._segments_emitted == 0:
            return self._find_first_segment_break(text, final=final)

        medium_break = self._find_first_boundary_after_min(
            text,
            boundaries=MEDIUM_BOUNDARIES,
            min_chars=self.config.medium_min_chars,
        )
        if medium_break:
            return medium_break

        weak_break = self._find_first_boundary_after_min(
            text,
            boundaries=WEAK_BOUNDARIES,
            min_chars=max(self.config.weak_min_chars, self.config.target_chars),
        )
        if weak_break:
            return weak_break

        if len(text) >= self.config.soft_max_chars:
            return self._find_soft_or_hard_break(
                text,
                max_chars=self.config.soft_max_chars,
                min_chars=self.config.weak_min_chars,
                allow_hard=len(text) >= self.config.hard_max_chars,
            )

        if final:
            return len(text)

        return 0

    def _find_first_segment_break(self, text: str, *, final: bool) -> int:
        if len(text.strip()) < self.config.first_min_chars:
            return len(text) if final else 0

        if final:
            return len(text)

        if len(text) >= self.config.first_hard_max:
            return self._find_soft_or_hard_break(
                text,
                max_chars=self.config.first_hard_max,
                min_chars=self.config.first_min_chars,
                allow_hard=True,
            )

        elapsed_ms = self._current_segment_elapsed_ms()
        if (
            len(text) < self.config.first_target_chars
            and elapsed_ms < self.config.first_deadline_ms
        ):
            return 0

        return self._find_natural_break_before(
            text,
            max_chars=min(len(text), self.config.first_target_chars),
            min_chars=self.config.first_min_chars,
        )

    def _find_first_boundary_after_min(
        self,
        text: str,
        *,
        boundaries: set[str],
        min_chars: int,
    ) -> int:
        for index, char in enumerate(text):
            if char in boundaries and index + 1 >= min_chars:
                return index + 1
        return 0

    def _find_soft_or_hard_break(
        self,
        text: str,
        *,
        max_chars: int,
        min_chars: int,
        allow_hard: bool,
    ) -> int:
        natural_break = self._find_natural_break_before(
            text,
            max_chars=min(len(text), max_chars),
            min_chars=min_chars,
        )
        if natural_break:
            return natural_break
        if allow_hard:
            return min(len(text), max_chars)
        return 0

    def _find_natural_break_before(self, text: str, *, max_chars: int, min_chars: int) -> int:
        upper = min(len(text), max_chars)
        for index in range(upper - 1, min_chars - 2, -1):
            char = text[index]
            if char in _NATURAL_BOUNDARIES or char.isspace():
                return index + 1
        return 0

    def _find_strong_break(self, text: str) -> int:
        index = 0
        while index < len(text):
            char = text[index]
            if char not in STRONG_BOUNDARIES:
                index += 1
                continue
            if char == ".":
                if text.startswith("...", index):
                    return index + 3
                if self._is_period_special_case(text, index):
                    index += 1
                    continue
            return index + 1
        return 0

    def _is_period_special_case(self, text: str, index: int) -> bool:
        previous_char = text[index - 1] if index > 0 else ""
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if previous_char.isdigit() and next_char.isdigit():
            return True

        token = _last_token(text[:index]).lower().rstrip(".")
        if token in _PREFIX_ABBREVIATIONS:
            return True
        if token in {"e.g", "i.e", "a.m", "p.m", "u.s", "u.k"}:
            return True
        if token in _COMMON_ABBREVIATIONS and next_char and next_char.islower():
            return True
        return bool(next_char and next_char.islower())

    def _consume_segment(self, break_index: int) -> str:
        raw_segment = self._buffer[:break_index]
        self._buffer = self._buffer[break_index:].lstrip()
        segment = _normalize_tts_segment(raw_segment)
        if segment:
            self._segments_emitted += 1
        if self._buffer.strip():
            self._segment_started_at = self._monotonic()
        else:
            self._segment_started_at = None
        return segment

    def _current_segment_elapsed_ms(self) -> int:
        if self._segment_started_at is None:
            return 0
        return int((self._monotonic() - self._segment_started_at) * 1000)


def _normalize_tts_segment(text: str) -> str:
    segment = _MARKDOWN_EMPHASIS_RE.sub(r"\2", text).strip()
    if not segment:
        return ""
    if not any(char.isalnum() for char in segment):
        return ""
    return segment


def _last_token(text: str) -> str:
    stripped = text.rstrip()
    if not stripped:
        return ""
    return stripped.split()[-1]
