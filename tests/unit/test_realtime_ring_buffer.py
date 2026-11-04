from __future__ import annotations

from src.realtime.audio.ring_buffer import RingBuffer


def test_ring_buffer_returns_data_in_chronological_order_after_wrap() -> None:
    rb = RingBuffer(sample_rate=16000, duration_ms=100)
    assert rb.capacity == 3200

    payload = bytes([index % 251 for index in range(5000)])
    rb.write(payload)

    assert rb.read_all() == payload[-rb.capacity :]


def test_ring_buffer_clear_resets_contents() -> None:
    rb = RingBuffer(sample_rate=16000, duration_ms=50)
    rb.write(b"\x01\x02\x03\x04")

    assert rb.read_all() == b"\x01\x02\x03\x04"

    rb.clear()

    assert rb.read_all() == b""
