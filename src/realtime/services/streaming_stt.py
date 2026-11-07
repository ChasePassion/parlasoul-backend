"""Progressive (streaming) STT for the realtime voice runtime.

The one-shot path sends the whole utterance to DashScope only after the user
stops talking, so the provider's processing time lands entirely on the critical
path (measured 2.4-7.2s locally).  This module keeps a pre-warmed realtime ASR
websocket alive while the session is listening and appends audio as it arrives,
so at end-of-turn only the tail has to be decoded.

Design notes:

* Audio is handed to an internal bounded queue and flushed by a writer task, so
  the 20 ms media callback never awaits network I/O.
* The websocket is opened ahead of time (``start()``) but a session that is not
  ready yet still accepts audio; frames queue up and flush once ready.
* Any failure surfaces as ``StreamingSTTError`` so the caller can fall back to
  the one-shot path without losing a turn.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from src.repositories.voice_gateway_repository import (
    _extract_event_message,
    _extract_event_type,
    _extract_qwen3_transcription_text,
    _merge_incremental_text,
    _new_event_id,
)

logger = logging.getLogger(__name__)

_TRANSCRIPT_EVENTS = {
    "conversation.item.input_audio_transcription.delta",
    "conversation.item.input_audio_transcription.text",
    "conversation.item.input_audio_transcription.completed",
}
_TERMINAL_EVENTS = {"session.finished"}


class StreamingSTTError(RuntimeError):
    """Raised when the streaming session cannot deliver a transcript."""


class StreamingSTTSession:
    """One utterance-scoped realtime ASR websocket."""

    def __init__(
        self,
        *,
        ws_url: str,
        api_key: str,
        sample_rate: int,
        connect_factory: Callable[..., Any],
        timeout_seconds: float,
        queue_frames: int = 600,
    ) -> None:
        self._ws_url = ws_url
        self._api_key = api_key
        self._sample_rate = sample_rate
        self._connect_factory = connect_factory
        self._timeout_seconds = timeout_seconds
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_frames)

        self._ws: Any | None = None
        self._ready = asyncio.Event()
        self._closed = asyncio.Event()
        self._writer_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._parts: list[str] = []
        self._failure: BaseException | None = None
        self._dropped_frames = 0
        self._appended_bytes = 0
        self._started_at = time.monotonic()
        self._first_partial_at: float | None = None

    # -- lifecycle ---------------------------------------------------------
    @property
    def first_partial_ms(self) -> int | None:
        if self._first_partial_at is None:
            return None
        return int((self._first_partial_at - self._started_at) * 1000)

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    @property
    def appended_audio_ms(self) -> int:
        return int(self._appended_bytes / 2 / self._sample_rate * 1000)

    async def start(self) -> None:
        """Connect and initialise the upstream ASR session."""
        session_payload = {
            "event_id": _new_event_id(),
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm",
                "sample_rate": self._sample_rate,
                "turn_detection": None,
            },
        }
        try:
            self._ws = await self._connect_factory(
                self._ws_url,
                api_key=self._api_key,
                extra_headers={"OpenAI-Beta": "realtime=v1"},
            ).__aenter__()
            await self._ws.send(json.dumps(session_payload))
            while True:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=self._timeout_seconds)
                if isinstance(raw, bytes):
                    continue
                event = json.loads(raw)
                event_type = _extract_event_type(event)
                if event_type in {"session.updated", "session.created"}:
                    break
                if event_type in {"error", "response.error"}:
                    raise StreamingSTTError(
                        _extract_event_message(event, "stt stream init failed")
                    )
        except StreamingSTTError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise StreamingSTTError(f"stt stream connect failed: {type(exc).__name__}") from exc

        self._ready.set()
        self._writer_task = asyncio.create_task(
            self._write_loop(), name="realtime.stt.stream.writer"
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="realtime.stt.stream.reader"
        )

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def usable(self) -> bool:
        """False once the upstream socket failed or the session was consumed."""
        return not self._closed.is_set() and self._failure is None

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self._started_at

    # -- producer side -----------------------------------------------------
    def append(self, pcm16: bytes) -> None:
        if not pcm16 or self._closed.is_set():
            return
        self._appended_bytes += len(pcm16)
        try:
            self._queue.put_nowait(pcm16)
        except asyncio.QueueFull:
            # Keep the newest audio: drop the oldest queued frame.
            self._dropped_frames += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(pcm16)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def finish(self) -> str:
        """Flush pending audio, close the upstream buffer and return the text."""
        if self._failure is not None:
            raise StreamingSTTError(str(self._failure))
        if not self._ready.is_set():
            await asyncio.wait_for(self._ready.wait(), timeout=self._timeout_seconds)
        try:
            await self._queue.put(None)
        except asyncio.QueueFull:
            self._queue.get_nowait()
            await self._queue.put(None)
        assert self._writer_task is not None
        await self._writer_task
        if self._ws is None:
            raise StreamingSTTError("stt stream closed")
        await self._ws.send(json.dumps({"event_id": _new_event_id(), "type": "input_audio_buffer.commit"}))
        await self._ws.send(json.dumps({"event_id": _new_event_id(), "type": "session.finish"}))
        assert self._reader_task is not None
        try:
            await asyncio.wait_for(self._reader_task, timeout=self._timeout_seconds)
        except TimeoutError as exc:
            raise StreamingSTTError("stt stream finalize timeout") from exc
        if self._failure is not None:
            raise StreamingSTTError(str(self._failure))
        return "".join(self._parts).strip()

    async def abort(self) -> None:
        self._closed.set()
        for task in (self._writer_task, self._reader_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._writer_task, self._reader_task):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        await self._close_ws()

    # -- internals ---------------------------------------------------------
    async def _write_loop(self) -> None:
        try:
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    return
                if self._ws is None:
                    return
                await self._ws.send(
                    json.dumps(
                        {
                            "event_id": _new_event_id(),
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode("ascii"),
                        }
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._failure = exc
            self._closed.set()

    async def _read_loop(self) -> None:
        try:
            while True:
                if self._ws is None:
                    return
                raw = await asyncio.wait_for(self._ws.recv(), timeout=self._timeout_seconds)
                if isinstance(raw, bytes):
                    continue
                event = json.loads(raw)
                event_type = _extract_event_type(event)
                if event_type is None:
                    continue
                if event_type in {"error", "response.error"}:
                    raise StreamingSTTError(
                        _extract_event_message(event, "stt stream upstream error")
                    )
                if event_type in _TRANSCRIPT_EVENTS or event_type in {
                    "response.done",
                    "session.finished",
                }:
                    text = _extract_qwen3_transcription_text(event)
                    if text:
                        if self._first_partial_at is None and event_type in _TRANSCRIPT_EVENTS:
                            self._first_partial_at = time.monotonic()
                        before = len(self._parts)
                        _merge_incremental_text(self._parts, text)
                        if len(self._parts) != before:
                            self._first_partial_at = self._first_partial_at or time.monotonic()
                if event_type in _TERMINAL_EVENTS:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._failure = exc
        finally:
            self._closed.set()

    async def _close_ws(self) -> None:
        if self._ws is None:
            return
        ws, self._ws = self._ws, None
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        close = getattr(ws, "__aexit__", None)
        if close is not None:
            try:
                await close(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def open_streaming_stt(
    *,
    repo: Any,
) -> StreamingSTTSession | None:
    """Build a streaming session from a VoiceGatewayRepository, or None."""
    model = str(getattr(repo, "_stt_model", "") or "")
    if not model.strip().lower().startswith("qwen3-asr-flash-realtime"):
        return None
    api_key = repo._require_api_key()  # noqa: SLF001
    ws_url = repo._stt_realtime_ws_url  # noqa: SLF001
    timeout_seconds = repo._stt_timeout_seconds()  # noqa: SLF001
    connect_factory = repo._connect  # noqa: SLF001
    url = f"{ws_url}?model={model}" if "?" not in ws_url else f"{ws_url}&model={model}"
    return StreamingSTTSession(
        ws_url=url,
        api_key=api_key,
        sample_rate=16000,
        connect_factory=connect_factory,
        timeout_seconds=timeout_seconds,
    )
