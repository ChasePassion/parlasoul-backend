"""Realtime WebRTC peer wrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiortc import RTCPeerConnection, RTCSessionDescription

from src.realtime.rtc.output_track import BotAudioTrack

logger = logging.getLogger(__name__)


class RealtimePeer:
    def __init__(self, *, session_id: str, pc: RTCPeerConnection, output_track: BotAudioTrack) -> None:
        self.session_id = session_id
        self.pc = pc
        self.output_track = output_track
        self.data_channel = None
        self._closed = False

    async def negotiate(self, *, offer: dict, on_datachannel, on_audio_track, on_connection_state_change=None) -> dict:
        started_at = time.perf_counter()

        def log_stage(stage: str, stage_started_at: float) -> None:
            logger.info(
                "Realtime peer negotiate stage completed: session_id=%s stage=%s elapsed_ms=%.3f total_elapsed_ms=%.3f",
                self.session_id,
                stage,
                (time.perf_counter() - stage_started_at) * 1000,
                (time.perf_counter() - started_at) * 1000,
            )

        @self.pc.on("datachannel")
        def _on_datachannel(channel):
            self.data_channel = channel
            on_datachannel(channel)

        @self.pc.on("track")
        def _on_track(track):
            if track.kind == "audio":
                on_audio_track(track)

        @self.pc.on("connectionstatechange")
        async def _on_state_change():
            logger.info(
                "Realtime peer state changed: session_id=%s state=%s",
                self.session_id,
                self.pc.connectionState,
            )
            if on_connection_state_change is not None:
                on_connection_state_change(self.pc.connectionState)

        remote = RTCSessionDescription(sdp=offer["sdp"], type=offer["type"])
        stage_started_at = time.perf_counter()
        await self.pc.setRemoteDescription(remote)
        log_stage("set_remote_description", stage_started_at)

        # Important: as the answering peer, prefer reusing the audio transceiver
        # created by the remote offer. If we blindly addTrack(), aiortc may create
        # a second sender that the browser never negotiated, which results in
        # subtitles working but no remote audio track on the browser side.
        audio_transceiver = next(
            (transceiver for transceiver in self.pc.getTransceivers() if transceiver.kind == "audio"),
            None,
        )
        if audio_transceiver is not None:
            audio_transceiver.direction = "sendrecv"
            audio_transceiver.sender.replaceTrack(self.output_track)
            logger.info(
                "Realtime peer attached bot audio to negotiated transceiver: session_id=%s mid=%s",
                self.session_id,
                audio_transceiver.mid,
            )
        else:
            self.pc.addTrack(self.output_track)
            logger.warning(
                "Realtime peer did not find negotiated audio transceiver, fallback addTrack: session_id=%s",
                self.session_id,
            )

        stage_started_at = time.perf_counter()
        answer = await self.pc.createAnswer()
        log_stage("create_answer", stage_started_at)
        stage_started_at = time.perf_counter()
        await self.pc.setLocalDescription(answer)
        log_stage("set_local_description", stage_started_at)
        logger.info(
            "Realtime peer negotiation completed: session_id=%s elapsed_ms=%.3f",
            self.session_id,
            (time.perf_counter() - started_at) * 1000,
        )
        return {
            "type": self.pc.localDescription.type,
            "sdp": self.pc.localDescription.sdp,
        }

    async def send_json(self, payload: dict) -> None:
        for _ in range(50):
            if self._closed:
                return
            if self.data_channel and self.data_channel.readyState == "open":
                self.data_channel.send(json.dumps(payload))
                return
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.pc.close()
