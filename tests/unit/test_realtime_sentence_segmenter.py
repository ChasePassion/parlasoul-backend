from __future__ import annotations

from src.realtime.services.sentence_segmenter import SentenceSegmenter, SentenceSegmenterConfig


def test_sentence_segmenter_splits_on_chinese_and_english_punctuation() -> None:
    segmenter = SentenceSegmenter(
        SentenceSegmenterConfig(min_length=4, max_length=80, language="auto")
    )

    segments = segmenter.feed("你好，今天过得怎么样？I am doing great.")

    assert segments == ["你好，今天过得怎么样？", "I am doing great."]
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


