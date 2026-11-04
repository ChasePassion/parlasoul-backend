"""Debounced user utterance commit gate for realtime voice turns."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

CommitReason = Literal["vad_silence", "manual_mute"]
DiscardReason = Literal["too_short", "empty", "cancelled"]


@dataclass(slots=True)
class TurnCommitGateConfig:
    commit_grace_ms: int = 400
    min_audio_ms: int = 300
    min_voiced_ms: int = 0
    sample_rate: int = 16000
    bytes_per_sample: int = 2


@dataclass(slots=True)
class PendingCommit:
    reason: CommitReason
    audio_ms: int
    voiced_ms: int
    task: asyncio.Task[None]


@dataclass(slots=True)
class TurnCommitGate:
    config: TurnCommitGateConfig
    on_commit: Callable[[bytes, CommitReason], Awaitable[None]]
    on_discard: Callable[[DiscardReason, int], Awaitable[None]]
    session_id: str | None = None
    chat_id: str | None = None
    character_id: str | None = None
    _chunks: list[bytes] = field(default_factory=list)
    _pending: PendingCommit | None = None
    _voiced_ms: int = 0
    _last_voiced_ms: int = 0
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def append(self, frame: bytes) -> None:
        if frame:
            self._chunks.append(frame)

    def reset_with_preroll(self, preroll: bytes | None) -> None:
        should_continue_pending_audio = self._pending is not None and self.has_audio
        self.cancel_pending("resumed_speech")
        if not should_continue_pending_audio:
            self._chunks.clear()
            self._voiced_ms = 0
        if preroll:
            self._chunks.append(preroll)

    def mark_voiced(self, duration_ms: int) -> None:
        if duration_ms > 0:
            self._voiced_ms += duration_ms

    def cancel_pending(self, reason: str) -> None:
        if self._pending is None:
            return
        pending = self._pending
        pending.task.cancel()
        self._pending = None
        logger.info(
            "Realtime turn commit cancelled: session_id=%s reason=%s audio_ms=%s",
            self.session_id,
            reason,
            pending.audio_ms,
            extra={
                "event": "realtime.turn_commit.cancelled",
                "chat_id": self.chat_id,
                "character_id": self.character_id,
            },
        )

    async def close(self) -> None:
        self.cancel_pending("gate_closed")
        tasks = list(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def schedule_commit(self, reason: CommitReason = "vad_silence", *, delay_ms: int | None = None) -> None:
        self.cancel_pending("reschedule")
        audio_ms = self.audio_ms
        wait_ms = self.config.commit_grace_ms if delay_ms is None else max(0, delay_ms)
        task = asyncio.create_task(self._commit_after(wait_ms / 1000, reason))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._pending = PendingCommit(
            reason=reason,
            audio_ms=audio_ms,
            voiced_ms=self._voiced_ms,
            task=task,
        )
        logger.info(
            "Realtime turn commit pending: session_id=%s reason=%s delay_ms=%s audio_ms=%s",
            self.session_id,
            reason,
            wait_ms,
            audio_ms,
            extra={
                "event": "realtime.turn_commit.pending",
                "chat_id": self.chat_id,
                "character_id": self.character_id,
            },
        )

    async def force_commit(self, reason: CommitReason = "manual_mute") -> None:
        self.cancel_pending("force_commit")
        await self._commit(reason)

    async def discard(self, reason: DiscardReason = "cancelled") -> None:
        self.cancel_pending(reason)
        audio_ms = self.audio_ms
        self._last_voiced_ms = self._voiced_ms
        self._chunks.clear()
        self._voiced_ms = 0
        await self.on_discard(reason, audio_ms)

    @property
    def audio_ms(self) -> int:
        total_bytes = sum(len(chunk) for chunk in self._chunks)
        samples = total_bytes // self.config.bytes_per_sample
        return int(samples / self.config.sample_rate * 1000)

    @property
    def has_audio(self) -> bool:
        return any(self._chunks)

    @property
    def has_pending_commit(self) -> bool:
        return self._pending is not None

    @property
    def voiced_ms(self) -> int:
        return self._voiced_ms

    @property
    def last_voiced_ms(self) -> int:
        return self._last_voiced_ms

    async def _commit_after(self, delay_seconds: float, reason: CommitReason) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            await self._commit(reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Realtime turn commit task failed: session_id=%s reason=%s error_type=%s",
                self.session_id,
                reason,
                type(exc).__name__,
                extra={
                    "event": "realtime.turn_commit.failed",
                    "chat_id": self.chat_id,
                    "character_id": self.character_id,
                },
            )
        finally:
            if self._pending and self._pending.task is asyncio.current_task():
                self._pending = None

    async def _commit(self, reason: CommitReason) -> None:
        audio = b"".join(self._chunks)
        audio_ms = self.audio_ms
        voiced_ms = self._voiced_ms
        self._last_voiced_ms = voiced_ms
        self._chunks.clear()
        self._voiced_ms = 0
        if not audio:
            await self.on_discard("empty", audio_ms)
            return
        if audio_ms < self.config.min_audio_ms or (
            self.config.min_voiced_ms > 0 and voiced_ms < self.config.min_voiced_ms
        ):
            await self.on_discard("too_short", audio_ms)
            return
        logger.info(
            "Realtime turn commit completed: session_id=%s reason=%s audio_ms=%s voiced_ms=%s",
            self.session_id,
            reason,
            audio_ms,
            voiced_ms,
            extra={
                "event": "realtime.turn_commit.committed",
                "chat_id": self.chat_id,
                "character_id": self.character_id,
            },
        )
        await self.on_commit(audio, reason)
