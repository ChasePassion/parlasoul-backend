"""WebRTC negotiation helpers."""

from __future__ import annotations

from aiortc import RTCConfiguration, RTCPeerConnection

from src.realtime.pipeline.audio_pacer import AudioPacer
from src.realtime.rtc.output_track import BotAudioTrack
from src.realtime.rtc.peer import RealtimePeer


def build_peer(
    *,
    session_id: str,
    configuration: RTCConfiguration | None = None,
    pacer_prebuffer_ms: int = 200,
    pacer_recovery_prebuffer_ms: int = 60,
    pacer_fadeout_ms: int = 50,
    pacer_max_buffer_ms: int = 2000,
) -> tuple[RealtimePeer, AudioPacer]:
    pc = RTCPeerConnection(configuration=configuration or RTCConfiguration(iceServers=[]))
    pacer = AudioPacer(
        prebuffer_ms=pacer_prebuffer_ms,
        recovery_prebuffer_ms=pacer_recovery_prebuffer_ms,
        fadeout_ms=pacer_fadeout_ms,
        max_buffer_ms=pacer_max_buffer_ms,
    )
    output_track = BotAudioTrack(pacer)
    return RealtimePeer(session_id=session_id, pc=pc, output_track=output_track), pacer
