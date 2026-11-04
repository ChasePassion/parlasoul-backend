from __future__ import annotations

import asyncio

import numpy as np
import pytest

from src.realtime.pipeline.audio_pacer import AudioPacer


@pytest.mark.asyncio
async def test_audio_pacer_accumulates_after_clear_before_playback() -> None:
    pacer = AudioPacer(sample_rate=48000, frame_ms=20, prebuffer_ms=40)
    frame_a = np.arange(pacer.frame_samples, dtype=np.int16).tobytes()
    frame_b = (np.arange(pacer.frame_samples, dtype=np.int16) + 1000).tobytes()

    await pacer.clear()
    await pacer.write_pcm16(frame_a)

    first = await pacer.read_frame()
    assert first == bytes(len(frame_a))

    await pacer.write_pcm16(frame_b)

    second = await pacer.read_frame()
    assert second == frame_a

    third = await pacer.read_frame()
    assert third == frame_b


@pytest.mark.asyncio
async def test_audio_pacer_begin_response_enables_prebuffer() -> None:
    pacer = AudioPacer(sample_rate=48000, frame_ms=20, prebuffer_ms=40)
    frame_a = np.arange(pacer.frame_samples, dtype=np.int16).tobytes()
    frame_b = (np.arange(pacer.frame_samples, dtype=np.int16) + 1000).tobytes()

    await pacer.begin_response()
    await pacer.write_pcm16(frame_a)

    first = await pacer.read_frame()
    assert first == bytes(len(frame_a))

    await pacer.write_pcm16(frame_b)

    assert await pacer.read_frame() == frame_a
    assert await pacer.read_frame() == frame_b


@pytest.mark.asyncio
async def test_audio_pacer_drain_waits_until_track_reads_last_frame() -> None:
    pacer = AudioPacer(sample_rate=48000, frame_ms=20, prebuffer_ms=20)
    frame = np.full(pacer.frame_samples, 7, dtype=np.int16).tobytes()

    assert await pacer.begin_response(1)
    assert await pacer.write_pcm16(frame, generation_id=1)
    assert await pacer.finish_response(1)
    drained = asyncio.create_task(pacer.wait_until_drained(1))
    await asyncio.sleep(0)
    assert not drained.done()

    assert await pacer.read_frame() == frame
    assert await asyncio.wait_for(drained, timeout=0.1) is True
    stats = await pacer.stats_snapshot()
    assert stats.first_audio_read_ms is not None
    assert stats.drained_ms is not None


@pytest.mark.asyncio
async def test_audio_pacer_backpressures_writer_instead_of_dropping_overflow() -> None:
    pacer = AudioPacer(
        sample_rate=48000,
        frame_ms=20,
        prebuffer_ms=20,
        max_buffer_ms=40,
    )
    frames = [
        np.full(pacer.frame_samples, value, dtype=np.int16).tobytes()
        for value in (1, 2, 3, 4)
    ]

    await pacer.begin_response()
    writer = asyncio.create_task(pacer.write_pcm16(b"".join(frames)))
    await asyncio.sleep(0)

    outputs = []
    for _ in frames:
        outputs.append(await pacer.read_frame())
        await asyncio.sleep(0)

    await asyncio.wait_for(writer, timeout=1)

    assert outputs == frames


@pytest.mark.asyncio
async def test_audio_pacer_retains_streaming_partial_frame_until_completed() -> None:
    pacer = AudioPacer(
        sample_rate=48000,
        frame_ms=20,
        prebuffer_ms=20,
        recovery_prebuffer_ms=20,
    )
    first_frame = np.full(pacer.frame_samples, 11, dtype=np.int16)
    first_partial = np.full(pacer.frame_samples // 2, 22, dtype=np.int16)
    second_partial = np.full(pacer.frame_samples // 2, 33, dtype=np.int16)

    await pacer.begin_response()
    await pacer.write_pcm16(np.concatenate((first_frame, first_partial)).tobytes())

    assert await pacer.read_frame() == first_frame.tobytes()

    underrun_frame = await pacer.read_frame()
    assert underrun_frame == bytes(pacer.frame_samples * 2)

    await pacer.write_pcm16(second_partial.tobytes())
    recovered = np.frombuffer(await pacer.read_frame(), dtype=np.int16)

    assert np.array_equal(recovered[: first_partial.size], first_partial)
    assert np.array_equal(recovered[first_partial.size :], second_partial)
    stats = await pacer.stats_snapshot()
    assert stats.underrun_count == 1
    assert stats.partial_pad_count == 0


@pytest.mark.asyncio
async def test_audio_pacer_rebuffers_after_streaming_underrun() -> None:
    pacer = AudioPacer(
        sample_rate=48000,
        frame_ms=20,
        prebuffer_ms=20,
        recovery_prebuffer_ms=40,
    )
    frame_a = np.full(pacer.frame_samples, 1, dtype=np.int16).tobytes()
    frame_b = np.full(pacer.frame_samples, 2, dtype=np.int16).tobytes()
    frame_c = np.full(pacer.frame_samples, 3, dtype=np.int16).tobytes()

    await pacer.begin_response()
    await pacer.write_pcm16(frame_a)

    assert await pacer.read_frame() == frame_a
    assert await pacer.read_frame() == bytes(len(frame_a))

    await pacer.write_pcm16(frame_b)
    assert await pacer.read_frame() == bytes(len(frame_a))

    await pacer.write_pcm16(frame_c)
    assert await pacer.read_frame() == frame_b
    assert await pacer.read_frame() == frame_c

    stats = await pacer.stats_snapshot()
    assert stats.underrun_count == 1
    assert stats.partial_pad_count == 0


@pytest.mark.asyncio
async def test_audio_pacer_finish_response_allows_final_partial_padding() -> None:
    pacer = AudioPacer(sample_rate=48000, frame_ms=20, prebuffer_ms=20)
    partial_samples = np.arange(pacer.frame_samples // 2, dtype=np.int16)

    await pacer.write_pcm16(partial_samples.tobytes())
    await pacer.finish_response()

    frame = await pacer.read_frame()
    samples = np.frombuffer(frame, dtype=np.int16)

    assert np.array_equal(samples[: partial_samples.size], partial_samples)
    assert np.array_equal(samples[partial_samples.size :], np.zeros(pacer.frame_samples - partial_samples.size, dtype=np.int16))
    stats = await pacer.stats_snapshot()
    assert stats.partial_pad_count == 1


@pytest.mark.asyncio
async def test_audio_pacer_fadeout_is_idempotent_and_fences_old_generation() -> None:
    pacer = AudioPacer(
        sample_rate=48000,
        frame_ms=20,
        prebuffer_ms=20,
        fadeout_ms=20,
    )
    frame_a = np.arange(pacer.frame_samples, dtype=np.int16).tobytes()
    frame_b = (np.arange(pacer.frame_samples, dtype=np.int16) + 1000).tobytes()

    assert await pacer.begin_response(1)
    assert await pacer.write_pcm16(frame_a + frame_b, generation_id=1)
    assert await pacer.clear_with_fadeout(1)
    assert not await pacer.clear_with_fadeout(1)
    assert not await pacer.write_pcm16(frame_a, generation_id=1)

    faded = await pacer.read_frame()
    assert faded != bytes(len(frame_a))
    assert await pacer.wait_until_drained(1)
    stats = await pacer.stats_snapshot()
    assert stats.fadeout_count == 1

    assert await pacer.begin_response(2)
    assert not await pacer.write_pcm16(frame_a, generation_id=1)
    assert await pacer.write_pcm16(frame_a, generation_id=2)
    assert await pacer.read_frame() == frame_a


@pytest.mark.asyncio
async def test_audio_pacer_reports_peak_buffer_without_changing_buffer_limits() -> None:
    pacer = AudioPacer(
        sample_rate=48000,
        frame_ms=20,
        prebuffer_ms=20,
        max_buffer_ms=2000,
    )
    frame = np.ones(pacer.frame_samples, dtype=np.int16).tobytes()

    assert await pacer.begin_response(7)
    assert await pacer.write_pcm16(frame * 3, generation_id=7)

    stats = await pacer.stats_snapshot()
    assert stats.buffered_ms == 60
    assert stats.peak_buffered_ms == 60
    assert pacer.prebuffer_ms == 20
    assert pacer.max_buffer_ms == 2000
