"""Sentence segmentation helpers for realtime TTS chunking.

This is a Python adaptation of the sentence segmentation strategy used in the
reference realtime project, trimmed to the current product scope:

- Chinese + English punctuation only
- Smart handling for abbreviations / numbers / URLs
- A latency-oriented fallback when punctuation has not arrived yet

Japanese-specific rules are intentionally excluded.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

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
    "inc",
    "ltd",
    "corp",
    "co",
    "no",
    "vol",
    "pg",
    "pp",
    "fig",
    "e.g",
    "i.e",
    "a.m",
    "p.m",
    "u.s",
    "u.k",
    "u.n",
    "st",
    "ave",
    "blvd",
    "rd",
    "dept",
    "est",
    "approx",
    "govt",
    "min",
    "max",
}

_PREFIX_ABBREVIATIONS = {
    "mr",
    "mrs",
    "ms",
    "dr",
    "prof",
    "sr",
    "jr",
    "st",
    "rev",
    "gen",
    "col",
    "lt",
    "sgt",
    "capt",
}

_NUMBER_PATTERNS = (
    re.compile(r"\d+\.\d*$"),
    re.compile(r"\$\d+\.\d*$"),
    re.compile(r"€\d+\.\d*$"),
    re.compile(r"£\d+\.\d*$"),
    re.compile(r"¥\d+\.\d*$"),
    re.compile(r"\d+\.\d*%$"),
    re.compile(r"v\d+\.\d*$"),
    re.compile(r"\d+\.\d+\.\d*$"),
    re.compile(r"\d{1,3}\.\d{1,3}\.$"),
)

_URL_EMAIL_PATTERNS = (
    re.compile(r"https?://\S*$"),
    re.compile(r"www\.\S*$"),
    re.compile(r"\S+@\S+\.\S*$"),
    re.compile(r"\S+\.(com|org|net|io|ai|cn)$", re.IGNORECASE),
)

_CHINESE_SENTENCE_ENDERS = {"。", "！", "？", "；", "…"}
_ENGLISH_SENTENCE_ENDERS = {".", "!", "?", ";"}
_SOFT_BREAK_PUNCTUATION = {",", "，", ":", "：", "、"}


@dataclass(slots=True)
class SentenceSegmenterConfig:
    min_length: int = 10
    max_length: int = 200
    language: str = "auto"
    enable_smart_punctuation: bool = True
    latency_flush_min_length: int = 14
    latency_flush_target_length: int = 28
    first_segment_timeout_ms: int = 300
    later_segment_timeout_ms: int = 700
    later_segment_min_length: int = 40
    later_segment_max_length: int = 120


class SentenceSegmenter:
    def __init__(
        self,
        config: SentenceSegmenterConfig | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or SentenceSegmenterConfig()
        self._buffer = ""
        self._monotonic = monotonic
        self._segment_started_at: float | None = None
        self._segments_emitted = 0

    @property
    def buffer(self) -> str:
        return self._buffer

    def feed(self, text: str) -> list[str]:
        if not text:
            return []
        if not self._buffer.strip():
            self._segment_started_at = self._monotonic()
        self._buffer += text
        return self._drain_ready_segments()

    def flush(self) -> list[str]:
        remaining = self._consume_segment(len(self._buffer)) if self._buffer.strip() else ""
        return [remaining] if remaining else []

    def flush_for_latency(self) -> str | None:
        if len(self._buffer.strip()) < max(
            self.config.min_length,
            self._current_latency_min_length(),
        ):
            return None

        target_length = self._current_latency_target_length()
        elapsed_ms = self._current_segment_elapsed_ms()
        timeout_ms = self._current_latency_timeout_ms()
        if (
            elapsed_ms < timeout_ms
            and len(self._buffer) < target_length + 8
            and len(self._buffer) < self.config.max_length
        ):
            return None

        break_index = self._find_latency_break(self._buffer, target_length=target_length)
        if break_index <= 0:
            return None

        segment = self._consume_segment(break_index)
        return segment or None

    def _drain_ready_segments(self) -> list[str]:
        segments: list[str] = []

        while self._buffer:
            break_index = self._find_sentence_break(self._buffer)
            if break_index <= 0:
                break

            segment = self._buffer[:break_index].strip()
            if len(segment) < self.config.min_length:
                break

            segments.append(segment)
            self._consume_segment(break_index)

        return segments

    def _find_sentence_break(self, text: str) -> int:
        if not text:
            return 0

        max_scan = min(len(text), self.config.max_length)
        for index, char in enumerate(text[:max_scan]):
            if not self._is_sentence_ender(char):
                continue

            if self.config.enable_smart_punctuation:
                before = text[: index + 1]
                after = text[index + 1 :]
                if self._is_special_case(before, after, char):
                    continue

            return index + 1

        if len(text) >= self.config.max_length:
            return self._find_forced_break(text)

        return 0

    def _find_forced_break(self, text: str) -> int:
        if soft_break := self._find_soft_break(text):
            return soft_break

        if space_break := self._find_space_break(text):
            return space_break

        return min(len(text), self.config.max_length)

    def _find_soft_break(self, text: str) -> int:
        for index in range(len(text) - 1, self.config.min_length - 1, -1):
            if text[index] in _SOFT_BREAK_PUNCTUATION:
                return index + 1
        return 0

    def _find_space_break(self, text: str) -> int:
        for index in range(len(text) - 1, self.config.min_length - 1, -1):
            if text[index].isspace():
                return index + 1
        return 0

    def _find_latency_break(self, text: str, *, target_length: int) -> int:
        upper = min(len(text) - 1, target_length)
        cut_index = -1

        for index, char in enumerate(text[: upper + 1]):
            if char in _SOFT_BREAK_PUNCTUATION or char.isspace():
                cut_index = index

        if cut_index < 0:
            if len(text) < target_length + 8:
                return 0
            cut_index = upper

        return cut_index + 1

    def _consume_segment(self, break_index: int) -> str:
        segment = self._buffer[:break_index].strip()
        self._buffer = self._buffer[break_index:]
        if segment:
            self._segments_emitted += 1
        if self._buffer.strip():
            self._segment_started_at = self._monotonic()
        else:
            self._segment_started_at = None
        return segment

    def _current_latency_timeout_ms(self) -> int:
        if self._segments_emitted == 0:
            return self.config.first_segment_timeout_ms
        return self.config.later_segment_timeout_ms

    def _current_latency_target_length(self) -> int:
        if self._segments_emitted == 0:
            return self.config.latency_flush_target_length
        return min(self.config.later_segment_max_length, self.config.max_length)

    def _current_latency_min_length(self) -> int:
        if self._segments_emitted == 0:
            return self.config.latency_flush_min_length
        return self.config.later_segment_min_length

    def _current_segment_elapsed_ms(self) -> int:
        if self._segment_started_at is None:
            return 0
        return int((self._monotonic() - self._segment_started_at) * 1000)

    def _is_sentence_ender(self, char: str) -> bool:
        if self.config.language == "en":
            return char in _ENGLISH_SENTENCE_ENDERS
        return char in _CHINESE_SENTENCE_ENDERS or char in _ENGLISH_SENTENCE_ENDERS

    def _is_special_case(self, text_before: str, text_after: str, punctuation: str) -> bool:
        if punctuation != ".":
            return False

        if _is_new_sentence_start(text_after) and not _is_prefix_abbreviation(text_before):
            return False

        if self._is_abbreviation(text_before):
            if _is_prefix_abbreviation(text_before):
                return True
            if _is_new_sentence_start(text_after):
                return False
            return True

        if any(pattern.search(text_before) for pattern in _NUMBER_PATTERNS):
            return not _is_new_sentence_start(text_after)

        if any(pattern.search(text_before) for pattern in _URL_EMAIL_PATTERNS):
            return not _is_new_sentence_start(text_after)

        if text_before.endswith(".."):
            return True

        return bool(text_after) and text_after[0].islower()

    def _is_abbreviation(self, text: str) -> bool:
        candidate = text.rstrip(".").strip()
        if not candidate:
            return False
        words = candidate.split()
        if not words:
            return False
        last_word = words[-1].lower().rstrip(".")
        return last_word in _COMMON_ABBREVIATIONS


def _is_new_sentence_start(text_after: str) -> bool:
    trimmed = text_after.lstrip(" \t")
    if not trimmed:
        return False
    return trimmed[0].isupper()


def _is_prefix_abbreviation(text: str) -> bool:
    candidate = text.rstrip(".").strip()
    if not candidate:
        return False
    words = candidate.split()
    if not words:
        return False
    return words[-1].lower() in _PREFIX_ABBREVIATIONS
