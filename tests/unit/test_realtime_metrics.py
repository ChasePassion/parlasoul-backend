from __future__ import annotations

import pytest

from src.realtime.session.metrics import sanitize_metric_payload


@pytest.mark.parametrize(
    "key",
    [
        "raw_audio_bytes",
        "pcm_payload",
        "full_transcript",
        "assistant_text",
        "prompt_content",
        "offer_sdp",
        "remote_ip_address",
    ],
)
def test_realtime_metrics_reject_privacy_unsafe_fields(key: str) -> None:
    with pytest.raises(ValueError):
        sanitize_metric_payload({key: "hidden"})


def test_realtime_metrics_reject_nested_or_binary_values() -> None:
    with pytest.raises(TypeError):
        sanitize_metric_payload({"timing": {"latency_ms": 1}})  # type: ignore[dict-item]
    with pytest.raises(TypeError):
        sanitize_metric_payload({"payload": b"\x01\x02"})  # type: ignore[dict-item]
