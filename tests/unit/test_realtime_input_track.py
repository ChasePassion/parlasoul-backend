import pytest
from aiortc.mediastreams import MediaStreamError

from src.realtime.rtc.input_track import InputTrackReader


class _EndedTrack:
    async def recv(self):
        raise MediaStreamError


@pytest.mark.asyncio
async def test_input_track_reader_treats_media_stream_error_as_normal_end() -> None:
    frames = []
    reader = InputTrackReader(
        track=_EndedTrack(),
        on_pcm16_frame=lambda pcm16, sample_rate: frames.append((pcm16, sample_rate)),
    )

    await reader._run()

    assert frames == []
