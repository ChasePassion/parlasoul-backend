"""Read-only realtime telemetry aggregations (content-free tables only).

Scope is an explicit allowlist of ``(observation_id, user_id)`` pairs — the
telemetry tenant boundary is never widened by ``system`` context. All
percentiles use PostgreSQL ``percentile_disc``. Media degradation thresholds
are uncalibrated in phase18: only raw distributions are reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Pipeline latency waterfall metrics (plan §5.1).
TURN_LATENCY_METRICS: tuple[str, ...] = (
    "endpointing_latency_ms",
    "stt_first_partial_latency_ms",
    "stt_final_latency_ms",
    "llm_ttft_ms",
    "first_speakable_chunk_latency_ms",
    "tts_ttfb_ms",
    "first_audio_enqueued_latency_ms",
    "server_first_audio_read_latency_ms",
    "ttfa_ms",
    "client_first_audio_played_latency_ms",
    "barge_in_stop_latency_ms",
)

# Raw media signals (no threshold → no degradation rate in phase18).
MEDIA_RAW_METRICS: tuple[str, ...] = (
    "rtt_ms",
    "inbound_jitter_ms",
    "packet_loss_ratio",
    "jitter_buffer_delay_ms",
    "concealed_ratio",
)


@dataclass(frozen=True)
class PercentileStat:
    status: str  # ok | no_data
    n: int = 0
    p50: float | None = None
    p95: float | None = None
    p99: float | None = None


def _scope_clause(prefix: str) -> str:
    return (
        f"EXISTS (SELECT 1 FROM unnest(CAST(:obs_ids AS uuid[]), "
        f"CAST(:user_ids AS uuid[])) AS scope(o, u) "
        f"WHERE scope.o = {prefix}.observation_id AND scope.u = {prefix}.user_id)"
    )


def _scope_params(observations: list[tuple[UUID, UUID]]) -> dict[str, Any]:
    return {
        "obs_ids": [str(observation_id) for observation_id, _ in observations],
        "user_ids": [str(user_id) for _, user_id in observations],
    }


async def turn_latency_percentiles(
    conn: AsyncConnection,
    observations: list[tuple[UUID, UUID]],
    metrics: tuple[str, ...] = TURN_LATENCY_METRICS,
) -> dict[str, PercentileStat]:
    if not observations:
        return {metric: PercentileStat(status="no_data") for metric in metrics}

    results: dict[str, PercentileStat] = {}
    scope = _scope_clause("m")
    params = _scope_params(observations)
    for metric in metrics:
        if metric not in TURN_LATENCY_METRICS:
            raise ValueError(f"unknown turn latency metric: {metric}")
        sql = text(
            f"""
            SELECT count(m.{metric}) AS n,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY m.{metric}) AS p50,
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY m.{metric}) AS p95,
                   percentile_disc(0.99) WITHIN GROUP (ORDER BY m.{metric}) AS p99
            FROM realtime_turn_metrics m
            WHERE {scope}
            """
        )
        row = (await conn.execute(sql, params)).one()
        n = int(row.n or 0)
        if n == 0:
            results[metric] = PercentileStat(status="no_data")
        else:
            results[metric] = PercentileStat(
                status="ok",
                n=n,
                p50=float(row.p50) if row.p50 is not None else None,
                p95=float(row.p95) if row.p95 is not None else None,
                p99=float(row.p99) if row.p99 is not None else None,
            )
    return results


async def media_raw_distributions(
    conn: AsyncConnection,
    observations: list[tuple[UUID, UUID]],
    metrics: tuple[str, ...] = MEDIA_RAW_METRICS,
) -> dict[str, dict[str, Any]]:
    """Raw distributions only — degradation thresholds stay uncalibrated."""
    if not observations:
        return {metric: {"status": "no_data", "n": 0} for metric in metrics}

    results: dict[str, dict[str, Any]] = {}
    scope = _scope_clause("s")
    params = _scope_params(observations)
    for metric in metrics:
        if metric not in MEDIA_RAW_METRICS:
            raise ValueError(f"unknown media metric: {metric}")
        sql = text(
            f"""
            SELECT count(s.{metric}) AS n,
                   min(s.{metric}) AS min_v,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY s.{metric}) AS p50,
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY s.{metric}) AS p95,
                   max(s.{metric}) AS max_v
            FROM realtime_webrtc_stats_samples s
            WHERE {scope}
            """
        )
        row = (await conn.execute(sql, params)).one()
        n = int(row.n or 0)
        if n == 0:
            results[metric] = {"status": "no_data", "n": 0}
        else:
            results[metric] = {
                "status": "ok",
                "n": n,
                "min": float(row.min_v),
                "p50": float(row.p50),
                "p95": float(row.p95),
                "max": float(row.max_v),
            }
    return results
