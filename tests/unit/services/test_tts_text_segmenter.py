from __future__ import annotations

from src.services.tts_text_segmenter import ConversationTTSSegmenter


def _feed_chunks(segmenter: ConversationTTSSegmenter, chunks: list[str]) -> list[str]:
    segments: list[str] = []
    for chunk in chunks:
        segments.extend(segmenter.feed(chunk))
    segments.extend(segmenter.flush())
    return segments


def test_conversation_tts_segmenter_does_not_fragment_streamed_reply() -> None:
    chunks = [
        "You're",
        " catching on",
        " fast.",
        " That instinct",
        " is worth more",
        " than a",
        " library of theory.",
        " Now go out there",
        " and test it",
        " - someone will say",
        " something fragile",
        " today,",
        " I guarantee it.",
        " Then you'll really",
        " see what I mean.",
        " 💪",
    ]

    segments = _feed_chunks(ConversationTTSSegmenter(), chunks)

    assert segments == [
        "You're catching on fast.",
        "That instinct is worth more than a library of theory.",
        (
            "Now go out there and test it - someone will say something fragile today, "
            "I guarantee it."
        ),
        "Then you'll really see what I mean.",
    ]
    assert all(any(ch.isalnum() for ch in segment) for segment in segments)


def test_conversation_tts_segmenter_uses_weak_boundary_only_after_threshold() -> None:
    segmenter = ConversationTTSSegmenter()

    assert segmenter.feed("One, two, three, four,") == []
    assert segmenter.feed(" five, six, seven, eight, nine, ten,") == [
        "One, two, three, four, five, six, seven, eight, nine,"
    ]
    assert segmenter.flush() == ["ten,"]


def test_conversation_tts_segmenter_first_packet_can_cut_by_time_deadline() -> None:
    now = 0.0

    def monotonic() -> float:
        return now

    segmenter = ConversationTTSSegmenter(monotonic=monotonic)

    assert segmenter.feed("This first response can") == []

    now = 0.8
    segments = segmenter.feed(" start once context arrives")

    assert segments == ["This first response can start once context"]
    assert segmenter.flush() == ["arrives"]
