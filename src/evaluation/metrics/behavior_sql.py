"""Behavior/online-KPI SQL aggregations (read-only, parameterized).

Rules from phase18:
- fixed UTC observation windows; samples whose observation lag has not
  elapsed are right-censored and excluded from the denominator;
- branch facts come from turns/candidates/chats, never from growth rollups;
- valid exchange = turn.state='OK' USER/CHARACTER turn with a final
  candidate that has no extra.error_code; static greetings excluded;
- aggregate KPIs require an explicit approved user scope; no scope means
  ``unavailable`` (never a cross-tenant sweep).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

DEFAULT_LOOKAHEAD_HOURS = 24


@dataclass(frozen=True)
class RatioMetric:
    status: str  # ok | no_data | unavailable
    numerator: int = 0
    denominator: int = 0
    value: float | None = None
    censored: int = 0
    excluded: int = 0


def _ratio(numerator: int, denominator: int, *, censored: int = 0, excluded: int = 0) -> RatioMetric:
    if denominator <= 0:
        return RatioMetric(status="no_data", censored=censored, excluded=excluded)
    return RatioMetric(
        status="ok",
        numerator=numerator,
        denominator=denominator,
        value=round(numerator / denominator, 6),
        censored=censored,
        excluded=excluded,
    )


_CONTINUATION_SQL = text(
    """
    WITH replies AS (
        SELECT t.id AS turn_id,
               t.primary_candidate_id AS candidate_id,
               t.created_at AS created_at
        FROM turns t
        JOIN candidates c ON c.id = t.primary_candidate_id
        WHERE t.author_type = 'CHARACTER'
          AND t.is_proactive = false
          AND t.state = 'OK'
          AND c.is_final = true
          AND (c.extra IS NULL OR c.extra->>'error_code' IS NULL)
          AND t.created_at >= :w_start
          AND t.created_at <= :w_end
    )
    SELECT
        count(*) FILTER (WHERE r.created_at <= :censor_boundary) AS eligible,
        count(*) FILTER (WHERE r.created_at > :censor_boundary) AS censored,
        count(*) FILTER (
            WHERE r.created_at <= :censor_boundary AND nxt.turn_id IS NOT NULL
        ) AS continued
    FROM replies r
    LEFT JOIN LATERAL (
        SELECT n.id AS turn_id
        FROM turns n
        JOIN candidates nc ON nc.id = n.primary_candidate_id
        WHERE n.parent_candidate_id = r.candidate_id
          AND n.author_type = 'USER'
          AND n.state = 'OK'
          AND nc.is_final = true
          AND (nc.extra IS NULL OR nc.extra->>'error_code' IS NULL)
          AND n.created_at >= r.created_at
          AND n.created_at <= r.created_at + make_interval(hours => :lookahead_hours)
          AND n.created_at <= :w_end
        LIMIT 1
    ) nxt ON true
    """
)


async def continuation_rate(
    conn: AsyncConnection,
    *,
    window_start: datetime,
    window_end: datetime,
    lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS,
) -> RatioMetric:
    censor_boundary = window_end - timedelta(hours=lookahead_hours)
    row = (
        await conn.execute(
            _CONTINUATION_SQL,
            {
                "w_start": window_start,
                "w_end": window_end,
                "censor_boundary": censor_boundary,
                "lookahead_hours": lookahead_hours,
            },
        )
    ).one()
    eligible = int(row.eligible or 0)
    censored = int(row.censored or 0)
    continued = int(row.continued or 0)
    return _ratio(continued, eligible, censored=censored)


_PROACTIVE_REPLY_SQL = text(
    """
    WITH proactive_chats AS (
        SELECT ch.id AS chat_id,
               ch.created_at AS created_at,
               d.id AS dispatch_id,
               d.status AS dispatch_status,
               d.payload_json->>'chat_id' AS payload_chat_id
        FROM chats ch
        LEFT JOIN LATERAL (
            SELECT * FROM proactive_message_dispatches pd
            WHERE pd.id::text = ch.meta->>'dispatch_id'
            LIMIT 1
        ) d ON true
        WHERE ch.origin = 'proactive'
          AND ch.created_at >= :w_start
          AND ch.created_at <= :w_end
          AND ch.state IN ('ACTIVE', 'ARCHIVED')
    ),
    classified AS (
        SELECT pc.chat_id,
               pc.created_at,
               CASE
                   WHEN pc.dispatch_id IS NULL THEN 'missing_dispatch'
                   WHEN pc.dispatch_status <> 'succeeded' THEN 'invalid_dispatch'
                   WHEN pc.payload_chat_id IS DISTINCT FROM pc.chat_id::text
                       THEN 'payload_mismatch'
                   ELSE 'valid'
               END AS dispatch_class
        FROM proactive_chats pc
    ),
    eligible AS (
        SELECT c.chat_id, c.created_at
        FROM classified c
        WHERE c.dispatch_class = 'valid'
    )
    SELECT
        (SELECT count(*) FROM classified WHERE dispatch_class = 'valid'
            AND created_at <= :censor_boundary) AS eligible,
        (SELECT count(*) FROM classified WHERE dispatch_class = 'valid'
            AND created_at > :censor_boundary) AS censored,
        (SELECT count(*) FROM classified WHERE dispatch_class = 'missing_dispatch') AS missing_dispatch,
        (SELECT count(*) FROM classified
            WHERE dispatch_class IN ('invalid_dispatch', 'payload_mismatch')) AS invalid_dispatch,
        (SELECT count(*)
         FROM eligible e
         WHERE e.created_at <= :censor_boundary
           AND EXISTS (
               SELECT 1 FROM turns n
               WHERE n.chat_id = e.chat_id
                 AND n.author_type = 'USER'
                 AND n.state = 'OK'
                 AND n.created_at >= e.created_at
                 AND n.created_at <= e.created_at + make_interval(hours => :lookahead_hours)
                 AND n.created_at <= :w_end
           )) AS replied
    """
)


async def proactive_reply_rate(
    conn: AsyncConnection,
    *,
    window_start: datetime,
    window_end: datetime,
    lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS,
) -> RatioMetric:
    censor_boundary = window_end - timedelta(hours=lookahead_hours)
    row = (
        await conn.execute(
            _PROACTIVE_REPLY_SQL,
            {
                "w_start": window_start,
                "w_end": window_end,
                "censor_boundary": censor_boundary,
                "lookahead_hours": lookahead_hours,
            },
        )
    ).one()
    eligible = int(row.eligible or 0)
    censored = int(row.censored or 0)
    replied = int(row.replied or 0)
    excluded = int(row.missing_dispatch or 0) + int(row.invalid_dispatch or 0)
    return _ratio(replied, eligible, censored=censored, excluded=excluded)


_VALID_TURNS_CTE = """
    valid_turns AS (
        SELECT t.id AS turn_id,
               t.chat_id,
               ch.user_id AS user_id,
               t.created_at,
               (t.created_at AT TIME ZONE 'UTC')::date AS utc_day
        FROM turns t
        JOIN chats ch ON ch.id = t.chat_id
        JOIN candidates c ON c.id = t.primary_candidate_id
        WHERE t.state = 'OK'
          AND t.author_type IN ('USER', 'CHARACTER')
          AND c.is_final = true
          AND (c.extra IS NULL OR c.extra->>'error_code' IS NULL)
          AND (c.extra IS NULL OR c.extra->>'source' IS DISTINCT FROM 'greeting')
          AND NOT (t.author_type = 'CHARACTER' AND t.is_proactive
                   AND t.parent_turn_id IS NULL)
          AND t.created_at >= :w_start
          AND t.created_at <= :w_end
          AND ch.user_id = ANY(CAST(:user_ids AS uuid[]))
    )
"""


async def online_kpis(
    conn: AsyncConnection,
    *,
    window_start: datetime,
    window_end: datetime,
    user_ids: list[UUID] | None,
    as_of: datetime,
) -> dict[str, Any]:
    """turns_per_session / active_turns_per_day / return_d1 / return_d7.

    ``user_ids`` is the approved scope; without it every KPI is
    ``unavailable``. Cohorts whose D+1/D+7 has not elapsed by ``as_of`` are
    right-censored and excluded from the denominator.
    """
    if user_ids is None:
        unavailable = RatioMetric(status="unavailable")
        return {
            "turns_per_session": unavailable,
            "active_turns_per_day": unavailable,
            "return_d1": unavailable,
            "return_d7": unavailable,
        }

    params: dict[str, Any] = {
        "w_start": window_start,
        "w_end": window_end,
        "user_ids": [str(uid) for uid in user_ids],
        "as_of": as_of,
    }

    session_sql = text(
        f"""
        WITH {_VALID_TURNS_CTE}
        SELECT (SELECT count(*) FROM valid_turns) AS total_turns,
               (SELECT count(DISTINCT chat_id) FROM valid_turns) AS active_chats
        """
    )
    row = (await conn.execute(session_sql, params)).one()
    turns_per_session = _ratio(int(row.total_turns or 0), int(row.active_chats or 0))

    day_sql = text(
        f"""
        WITH {_VALID_TURNS_CTE}
        SELECT (SELECT count(*) FROM valid_turns) AS total_turns,
               (SELECT count(DISTINCT utc_day) FROM valid_turns) AS active_days
        """
    )
    row = (await conn.execute(day_sql, params)).one()
    active_turns_per_day = _ratio(int(row.total_turns or 0), int(row.active_days or 0))

    returns: dict[str, RatioMetric] = {}
    for key, offset in (("return_d1", 1), ("return_d7", 7)):
        return_sql = text(
            f"""
            WITH {_VALID_TURNS_CTE},
            cohort AS (
                SELECT DISTINCT vt.user_id, vt.utc_day AS cohort_day
                FROM valid_turns vt
            ),
            eligible AS (
                SELECT c.user_id, c.cohort_day
                FROM cohort c
                WHERE c.cohort_day + CAST(:offset AS integer)
                      <= (:as_of AT TIME ZONE 'UTC')::date
            ),
            returned AS (
                SELECT DISTINCT e.user_id, e.cohort_day
                FROM eligible e
                JOIN valid_turns vt
                  ON vt.user_id = e.user_id
                 AND vt.utc_day = e.cohort_day + CAST(:offset AS integer)
            )
            SELECT (SELECT count(*) FROM eligible) AS denominator,
                   (SELECT count(*) FROM returned) AS numerator,
                   (SELECT count(*) FROM cohort) - (SELECT count(*) FROM eligible) AS censored
            """
        )
        row = (await conn.execute(return_sql, {**params, "offset": offset})).one()
        returns[key] = _ratio(
            int(row.numerator or 0),
            int(row.denominator or 0),
            censored=int(row.censored or 0),
        )

    return {
        "turns_per_session": turns_per_session,
        "active_turns_per_day": active_turns_per_day,
        "return_d1": returns["return_d1"],
        "return_d7": returns["return_d7"],
    }
