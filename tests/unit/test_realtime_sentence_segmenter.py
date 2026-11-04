from __future__ import annotations

from src.realtime.services.sentence_segmenter import SentenceSegmenter, SentenceSegmenterConfig


def test_sentence_segmenter_splits_on_chinese_and_english_punctuation() -> None:
    segmenter = SentenceSegmenter(
        SentenceSegmenterConfig(min_length=4, max_length=80, language="auto")
    )

    segments = segmenter.feed("你好，今天过得怎么样？I am doing great.")

    assert segments == ["你好，今天过得怎么样？", "I am doing great."]
    assert segmenter.buffer == ""


def test_sentence_segmenter_keeps_prefix_abbreviation_with_following_name() -> None:
    segmenter = SentenceSegmenter(
        SentenceSegmenterConfig(min_length=4, max_length=80, language="en")
    )

    segments = segmenter.feed("Dr. Smith is ready to help.")

    assert segments == ["Dr. Smith is ready to help."]
    assert segmenter.buffer == ""


def test_sentence_segmenter_latency_flush_emits_partial_segment_without_punctuation() -> None:
    segmenter = SentenceSegmenter(
        SentenceSegmenterConfig(
            min_length=6,
            max_length=80,
            language="auto",
            latency_flush_min_length=10,
            latency_flush_target_length=20,
        )
    )

    ready = segmenter.feed("This response has no punctuation yet but should still")
    latency_segment = segmenter.flush_for_latency()

    assert ready == []
    assert latency_segment is not None
    assert len(latency_segment) >= 10
    assert segmenter.buffer != ""


def test_sentence_segmenter_first_segment_latency_waits_for_timeout_or_target() -> None:
    now = 0.0

    def monotonic() -> float:
        return now

    segmenter = SentenceSegmenter(
        SentenceSegmenterConfig(
            min_length=4,
            max_length=120,
            language="auto",
            latency_flush_min_length=6,
            latency_flush_target_length=40,
            first_segment_timeout_ms=300,
        ),
        monotonic=monotonic,
    )

    ready = segmenter.feed("This response starts slowly")

    assert ready == []
    assert segmenter.flush_for_latency() is None

    now = 0.35
    segment = segmenter.flush_for_latency()

    assert segment == "This response starts"
    assert segmenter.buffer.strip() == "slowly"


def test_sentence_segmenter_later_segments_use_larger_latency_target() -> None:
    segmenter = SentenceSegmenter(
        SentenceSegmenterConfig(
            min_length=4,
            max_length=120,
            language="auto",
            latency_flush_min_length=6,
            latency_flush_target_length=12,
            later_segment_max_length=80,
            first_segment_timeout_ms=10_000,
            later_segment_timeout_ms=10_000,
        )
    )

    first = segmenter.feed("Short first sentence. ")
    later = segmenter.feed("This later segment is longer and should not flush at the first target")

    assert first == ["Short first sentence."]
    assert later == []
    assert segmenter.flush_for_latency() is None


def test_sentence_segmenter_later_latency_waits_for_later_min_length() -> None:
    now = 0.0

    def monotonic() -> float:
        return now

    segmenter = SentenceSegmenter(
        SentenceSegmenterConfig(
            min_length=4,
            max_length=120,
            language="auto",
            latency_flush_min_length=6,
            latency_flush_target_length=12,
            later_segment_min_length=40,
            later_segment_max_length=80,
            first_segment_timeout_ms=100,
            later_segment_timeout_ms=100,
        ),
        monotonic=monotonic,
    )

    assert segmenter.feed("Short first sentence. ") == ["Short first sentence."]
    assert segmenter.feed("short later chunk") == []

    now = 0.2
    assert segmenter.flush_for_latency() is None

    segmenter.feed(" that now has enough words to reach the configured minimum,")
    segment = segmenter.flush_for_latency()

    assert segment is not None
    assert len(segment) >= 40


def test_sentence_segmenter_flush_returns_remaining_tail() -> None:
    segmenter = SentenceSegmenter(
        SentenceSegmenterConfig(min_length=4, max_length=80, language="auto")
    )

    _ = segmenter.feed("This tail has no final punctuation")
    remaining = segmenter.flush()

    assert remaining == ["This tail has no final punctuation"]
    assert segmenter.buffer == ""
