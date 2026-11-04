from __future__ import annotations

from src.realtime.audio.debug_capture import RealtimeAudioDebugCapture


def test_realtime_audio_debug_capture_cannot_write_audio_or_text(tmp_path) -> None:
    capture = RealtimeAudioDebugCapture.create_if_enabled(
        enabled=True,
        base_dir=str(tmp_path),
        session_id="rt_test",
        chat_id="chat-id",
        character_id="character-id",
        pacer_sample_rate=48000,
        max_bytes_per_file=1_000_000,
    )

    assert capture is None

    compatibility_shim = RealtimeAudioDebugCapture()
    compatibility_shim.record_tts_segment(index=1, text="must not be retained")
    compatibility_shim.write_tts_raw(b"\x01\x00", sample_rate=24000)
    compatibility_shim.write_pacer_output(b"\x01\x00", sample_rate=48000)
    compatibility_shim.finish("test_done")

    assert list(tmp_path.iterdir()) == []
