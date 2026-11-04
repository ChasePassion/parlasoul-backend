"""Local DashScope realtime-TTS stand-in for the voice lab.

The configured DashScope key has no TTS entitlement ("Model not exist") and the
MiniMax account has no balance, so no real TTS provider can synthesize audio on
this machine.  This server implements the small subset of the DashScope realtime
TTS websocket protocol the backend uses, and synthesizes speech locally with the
Windows SAPI voices.

Point the backend at it with:
    DASHSCOPE_TTS_WEBSOCKET_API_URL=ws://127.0.0.1:8897/api-ws/v1/realtime

Protocol handled (client -> server):
    session.update            -> session.updated
    input_text_buffer.append  -> response.audio.delta* + response.done
    session.finish            -> session.finished

Audio is 24 kHz, 16-bit, mono PCM, base64 encoded in `delta`, matching
`audio/pcm;rate=24000`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import tempfile
import uuid
from pathlib import Path

import websockets

SAMPLE_RATE = 24000
CHUNK_MS = 100
BYTES_PER_CHUNK = SAMPLE_RATE * 2 * CHUNK_MS // 1000


def _sapi_synthesize(text: str, voice: str | None) -> bytes:
    """Render text to raw PCM16 mono @ 24 kHz using SAPI."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    tmp = Path(tempfile.gettempdir()) / f"voice-lab-tts-{uuid.uuid4().hex}.wav"
    try:
        fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
        # 22 = SAFT24kHz16Bit, 16 = 16-bit, mono default
        fmt.Type = 22
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Format = fmt
        stream.Open(str(tmp), 3, False)  # 3 = SSFMCreateForWrite
        voice_obj = win32com.client.Dispatch("SAPI.SpVoice")
        if voice:
            for token in voice_obj.GetVoices():
                if voice.lower() in token.GetDescription().lower():
                    voice_obj.Voice = token
                    break
        voice_obj.AudioOutputStream = stream
        voice_obj.Rate = 0
        voice_obj.Speak(text)
        stream.Close()
        raw = tmp.read_bytes()
        # Skip the RIFF header: find the "data" chunk.
        at = raw.find(b"data")
        if at == -1:
            return b""
        size = int.from_bytes(raw[at + 4 : at + 8], "little")
        return raw[at + 8 : at + 8 + size]
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        pythoncom.CoUninitialize()


async def _speak(ws, text: str, voice: str | None) -> None:
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    try:
        pcm = await asyncio.to_thread(_sapi_synthesize, text, voice)
    except Exception as exc:  # noqa: BLE001
        await ws.send(json.dumps({"type": "error", "message": f"sapi failed: {exc}"}))
        return
    for offset in range(0, len(pcm), BYTES_PER_CHUNK):
        chunk = pcm[offset : offset + BYTES_PER_CHUNK]
        await ws.send(
            json.dumps(
                {
                    "type": "response.audio.delta",
                    "response_id": response_id,
                    "delta": base64.b64encode(chunk).decode("ascii"),
                }
            )
        )
        await asyncio.sleep(0.02)
    await ws.send(
        json.dumps(
            {
                "type": "response.done",
                "response_id": response_id,
                "usage": {"characters": len(text)},
            }
        )
    )


async def handle(ws) -> None:
    voice: str | None = None
    pending: list[asyncio.Task] = []
    async for raw in ws:
        if isinstance(raw, bytes):
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "session.update":
            session = event.get("session") or {}
            requested = session.get("voice") or ""
            # The clone/system voice ids are not SAPI voice names; default to Zira.
            voice = "Zira" if not any(
                name in requested.lower() for name in ("zira", "david", "huihui")
            ) else requested
            await ws.send(json.dumps({"type": "session.updated", "session": session}))
        elif kind == "input_text_buffer.append":
            text = (event.get("text") or "").strip()
            if text:
                task = asyncio.create_task(_speak(ws, text, voice))
                pending.append(task)
                pending = [t for t in pending if not t.done()]
        elif kind == "session.finish":
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await ws.send(json.dumps({"type": "session.finished"}))
            break


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8897)
    args = parser.parse_args()
    async with websockets.serve(handle, "127.0.0.1", args.port, max_size=8 * 1024 * 1024):
        print(f"[fake-tts] ws://127.0.0.1:{args.port}/api-ws/v1/realtime", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    asyncio.run(main())
