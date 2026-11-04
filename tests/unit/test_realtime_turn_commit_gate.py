from __future__ import annotations

import asyncio

import numpy as np
import pytest

from src.realtime.pipeline.turn_commit_gate import (
    CommitReason,
    DiscardReason,
    TurnCommitGate,
    TurnCommitGateConfig,
)


def _audio_ms(ms: int, *, sample_rate: int = 16000, value: int = 1) -> bytes:
    samples = np.full(sample_rate * ms // 1000, value, dtype=np.int16)
    return samples.tobytes()


@pytest.mark.asyncio
async def test_turn_commit_gate_schedules_commit_without_immediate_stt() -> None:
    committed: list[tuple[bytes, CommitReason]] = []
    discarded: list[tuple[DiscardReason, int]] = []

    async def on_commit(audio: bytes, reason: CommitReason) -> None:
        committed.append((audio, reason))

    async def on_discard(reason: DiscardReason, audio_ms: int) -> None:
        discarded.append((reason, audio_ms))

    gate = TurnCommitGate(
        config=TurnCommitGateConfig(commit_grace_ms=20, min_audio_ms=10),
        on_commit=on_commit,
        on_discard=on_discard,
    )

    gate.append(_audio_ms(40))
    gate.schedule_commit("vad_silence")

    assert committed == []
    assert discarded == []

    await asyncio.sleep(0.04)

    assert len(committed) == 1
    assert committed[0][1] == "vad_silence"
    assert discarded == []


@pytest.mark.asyncio
async def test_turn_commit_gate_resumed_speech_preserves_pending_audio() -> None:
    committed: list[bytes] = []

    async def on_commit(audio: bytes, _: CommitReason) -> None:
        committed.append(audio)

    async def on_discard(_: DiscardReason, __: int) -> None:
        return None

    gate = TurnCommitGate(
        config=TurnCommitGateConfig(commit_grace_ms=30, min_audio_ms=10),
        on_commit=on_commit,
        on_discard=on_discard,
    )
    first_audio = _audio_ms(40, value=1)
    resumed_preroll = _audio_ms(20, value=2)
    resumed_tail = _audio_ms(40, value=3)

    gate.append(first_audio)
    gate.schedule_commit("vad_silence")
    gate.reset_with_preroll(resumed_preroll)
    gate.append(resumed_tail)

    await asyncio.sleep(0.05)
    assert committed == []

    gate.schedule_commit("vad_silence")
    await asyncio.sleep(0.05)

    assert committed == [first_audio + resumed_preroll + resumed_tail]


@pytest.mark.asyncio
async def test_turn_commit_gate_discards_short_utterance() -> None:
    committed: list[bytes] = []
    discarded: list[tuple[DiscardReason, int]] = []

    async def on_commit(audio: bytes, _: CommitReason) -> None:
        committed.append(audio)

    async def on_discard(reason: DiscardReason, audio_ms: int) -> None:
        discarded.append((reason, audio_ms))

    gate = TurnCommitGate(
        config=TurnCommitGateConfig(commit_grace_ms=0, min_audio_ms=100),
        on_commit=on_commit,
        on_discard=on_discard,
    )

    gate.append(_audio_ms(40))
    await gate.force_commit("manual_mute")

    assert committed == []
    assert discarded == [("too_short", 40)]


@pytest.mark.asyncio
async def test_turn_commit_gate_preroll_cannot_satisfy_minimum_voiced_duration() -> None:
    committed: list[bytes] = []
    discarded: list[tuple[DiscardReason, int]] = []

    async def on_commit(audio: bytes, _: CommitReason) -> None:
        committed.append(audio)

    async def on_discard(reason: DiscardReason, audio_ms: int) -> None:
        discarded.append((reason, audio_ms))

    gate = TurnCommitGate(
        config=TurnCommitGateConfig(
            commit_grace_ms=0,
            min_audio_ms=300,
            min_voiced_ms=200,
        ),
        on_commit=on_commit,
        on_discard=on_discard,
    )

    gate.reset_with_preroll(_audio_ms(300))
    gate.mark_voiced(32)
    await gate.force_commit("manual_mute")

    assert committed == []
    assert discarded == [("too_short", 300)]
    assert gate.last_voiced_ms == 32


@pytest.mark.asyncio
async def test_turn_commit_gate_force_commit_for_mute() -> None:
    committed: list[tuple[bytes, CommitReason]] = []

    async def on_commit(audio: bytes, reason: CommitReason) -> None:
        committed.append((audio, reason))

    async def on_discard(_: DiscardReason, __: int) -> None:
        return None

    gate = TurnCommitGate(
        config=TurnCommitGateConfig(commit_grace_ms=100, min_audio_ms=10),
        on_commit=on_commit,
        on_discard=on_discard,
    )
    audio = _audio_ms(40)

    gate.append(audio)
    gate.schedule_commit("vad_silence")
    await gate.force_commit("manual_mute")

    assert committed == [(audio, "manual_mute")]
