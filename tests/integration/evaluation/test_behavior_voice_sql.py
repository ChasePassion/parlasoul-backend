"""Seeded-data + empty-data integration tests for behavior/voice SQL.

Uses an isolated per-test schema (tables remapped from SQLModel metadata; RLS
not involved because the CLI source-reader boundary is exercised separately).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.sql import text as sql_text

from src.core.config import settings
from src.db.models import (
    Candidate,
    Character,
    Chat,
    ProactiveMessageDispatch,
    Turn,
    User,
)
from src.evaluation.metrics.behavior_sql import (
    continuation_rate,
    proactive_reply_rate,
)

_TABLES = [
    User.__table__,
    Character.__table__,
    Chat.__table__,
    Turn.__table__,
    Candidate.__table__,
    ProactiveMessageDispatch.__table__,
]

W_START = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
W_END = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)


@pytest.fixture()
async def seeded_conn(ensure_pg_enum_types) -> AsyncIterator[AsyncConnection]:
    if not settings.DATABASE_URL.startswith("postgresql+asyncpg://"):
        pytest.skip("requires postgresql+asyncpg DATABASE_URL")

    schema = f"test_eval_sql_{uuid.uuid4().hex}"
    quoted = '"' + schema + '"'
    connect_args = dict(settings.database_connect_args)
    server_settings = dict(connect_args.get("server_settings", {}))
    server_settings["search_path"] = f"{schema},public"
    connect_args["server_settings"] = server_settings

    engine = create_async_engine(settings.DATABASE_URL, echo=False, connect_args=connect_args)
    isolated_meta = MetaData()
    isolated_tables = [t.to_metadata(isolated_meta, schema=schema) for t in _TABLES]

    admin_engine = create_async_engine(
        settings.DATABASE_URL, echo=False, connect_args=settings.database_connect_args
    )
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(sql_text(f"CREATE SCHEMA {quoted}"))
        async with engine.begin() as conn:
            await ensure_pg_enum_types(conn)
            await conn.run_sync(isolated_meta.create_all, tables=isolated_tables)
            await _seed(conn)
        async with engine.connect() as conn:
            yield conn
    finally:
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(sql_text(f"DROP SCHEMA IF EXISTS {quoted} CASCADE"))
        await admin_engine.dispose()


def _ts(hours: float) -> datetime:
    return W_START + timedelta(hours=hours)


async def _seed(conn: AsyncConnection) -> None:
    user_id = uuid.uuid4()
    character_id = uuid.uuid4()

    await conn.execute(
        User.__table__.insert(),
        [
            {"id": user_id, "email": f"u1-{uuid.uuid4().hex}@example.com"},
        ],
    )
    await conn.execute(
        Character.__table__.insert(),
        [
            {
                "id": character_id,
                "creator_id": user_id,
                "name": "Fixture Char",
                "description": "A fixture character for evaluation SQL tests.",
                "visibility": "PUBLIC",
                "system_prompt": "be kind",
                "greeting_message": "hi",
            }
        ],
    )

    def chat_row(chat_id: uuid.UUID, *, origin: str = "user", meta: dict | None = None, hours: float = 1.0, state: str = "ACTIVE"):
        return {
            "id": chat_id,
            "user_id": user_id,
            "character_id": character_id,
            "type": "ONE_ON_ONE",
            "state": state,
            "visibility": "PRIVATE",
            "origin": origin,
            "title": "t",
            "created_at": _ts(hours),
            "meta": meta or {},
        }

    def turn_row(turn_id, chat_id, *, turn_no, author, state="OK", proactive=False, candidate_id=None, parent_candidate_id=None, hours=1.0):
        return {
            "id": turn_id,
            "chat_id": chat_id,
            "turn_no": turn_no,
            "author_type": author,
            "state": state,
            "is_proactive": proactive,
            "primary_candidate_id": candidate_id,
            "parent_candidate_id": parent_candidate_id,
            "created_at": _ts(hours),
        }

    def candidate_row(candidate_id, turn_id, *, content="hello", final=True, extra=None):
        return {
            "id": candidate_id,
            "turn_id": turn_id,
            "candidate_no": 1,
            "content": content,
            "is_final": final,
            "extra": extra or {},
            "created_at": _ts(1),
        }

    rows_chats = []
    rows_turns = []
    rows_candidates = []

    # --- normal chat: assistant reply continued by user ---------------------
    chat1 = uuid.uuid4()
    reply_turn = uuid.uuid4()
    reply_cand = uuid.uuid4()
    user_turn = uuid.uuid4()
    user_cand = uuid.uuid4()
    rows_chats.append(chat_row(chat1, hours=0.5))
    rows_candidates.append(candidate_row(reply_cand, reply_turn))
    rows_turns.append(
        turn_row(reply_turn, chat1, turn_no=1, author="CHARACTER", candidate_id=reply_cand, hours=1.0)
    )
    rows_candidates.append(candidate_row(user_cand, user_turn, content="user says"))
    rows_turns.append(
        turn_row(
            user_turn, chat1, turn_no=2, author="USER", candidate_id=user_cand,
            parent_candidate_id=reply_cand, hours=1.5,
        )
    )

    # --- assistant reply without continuation --------------------------------
    chat2 = uuid.uuid4()
    lonely_turn = uuid.uuid4()
    lonely_cand = uuid.uuid4()
    rows_chats.append(chat_row(chat2, hours=1.8))
    rows_candidates.append(candidate_row(lonely_cand, lonely_turn))
    rows_turns.append(
        turn_row(lonely_turn, chat2, turn_no=1, author="CHARACTER", candidate_id=lonely_cand, hours=2.0)
    )

    # --- reply inside censor band (last `lookahead` hours of window) --------
    chat3 = uuid.uuid4()
    censored_turn = uuid.uuid4()
    censored_cand = uuid.uuid4()
    rows_chats.append(chat_row(chat3, hours=23.2))
    rows_candidates.append(candidate_row(censored_cand, censored_turn))
    rows_turns.append(
        turn_row(censored_turn, chat3, turn_no=1, author="CHARACTER", candidate_id=censored_cand, hours=23.5)
    )

    # --- proactive assistant reply (excluded from continuation) --------------
    chat4 = uuid.uuid4()
    proactive_turn = uuid.uuid4()
    proactive_cand = uuid.uuid4()
    rows_chats.append(chat_row(chat4, hours=2.5, origin="proactive"))
    rows_candidates.append(candidate_row(proactive_cand, proactive_turn))
    rows_turns.append(
        turn_row(proactive_turn, chat4, turn_no=1, author="CHARACTER", candidate_id=proactive_cand, proactive=True, hours=3.0)
    )

    # --- reply with error_code candidate (excluded) ---------------------------
    chat5 = uuid.uuid4()
    error_turn = uuid.uuid4()
    error_cand = uuid.uuid4()
    rows_chats.append(chat_row(chat5, hours=3.5))
    rows_candidates.append(candidate_row(error_cand, error_turn, extra={"error_code": "llm_service_error"}))
    rows_turns.append(
        turn_row(error_turn, chat5, turn_no=1, author="CHARACTER", candidate_id=error_cand, hours=4.0)
    )

    # --- proactive reply rate fixtures -----------------------------------------
    dispatch_rows = []

    def add_proactive_chat(
        hours: float, *, with_dispatch: bool, dispatch_status: str = "succeeded", state: str = "ACTIVE"
    ) -> uuid.UUID:
        chat_id = uuid.uuid4()
        meta: dict = {}
        if with_dispatch:
            dispatch_id = uuid.uuid4()
            dispatch_rows.append(
                {
                    "id": dispatch_id,
                    "user_id": user_id,
                    "slot_at": _ts(hours - 0.5),
                    "slot_label": "morning",
                    "timezone": "UTC",
                    "status": dispatch_status,
                    "payload_json": {"chat_id": str(chat_id)},
                    "next_attempt_at": _ts(hours - 0.4),
                }
            )
            meta["dispatch_id"] = str(dispatch_id)
        rows_chats.append(chat_row(chat_id, origin="proactive", meta=meta, hours=hours, state=state))
        return chat_id

    # P1 valid + replied
    p1 = add_proactive_chat(7.0, with_dispatch=True)
    p1_turn = uuid.uuid4()
    p1_cand = uuid.uuid4()
    rows_candidates.append(candidate_row(p1_cand, p1_turn, content="proactive msg"))
    rows_turns.append(turn_row(p1_turn, p1, turn_no=1, author="CHARACTER", candidate_id=p1_cand, proactive=True, hours=7.0))
    p1_user_turn = uuid.uuid4()
    p1_user_cand = uuid.uuid4()
    rows_candidates.append(candidate_row(p1_user_cand, p1_user_turn, content="user reply"))
    rows_turns.append(turn_row(p1_user_turn, p1, turn_no=2, author="USER", candidate_id=p1_user_cand, hours=7.2))

    # P2 valid, no reply
    add_proactive_chat(8.0, with_dispatch=True)
    # P3 missing dispatch -> excluded
    add_proactive_chat(9.0, with_dispatch=False)
    # P4 failed dispatch -> excluded
    add_proactive_chat(10.0, with_dispatch=True, dispatch_status="failed")
    # P5 valid but censored (inside last hour with lookahead=1h)
    add_proactive_chat(23.7, with_dispatch=True)
    # P6 archived + replied
    p6 = add_proactive_chat(11.0, with_dispatch=True, state="ARCHIVED")
    p6_turn = uuid.uuid4()
    p6_cand = uuid.uuid4()
    rows_candidates.append(candidate_row(p6_cand, p6_turn, content="proactive msg 6"))
    rows_turns.append(turn_row(p6_turn, p6, turn_no=1, author="CHARACTER", candidate_id=p6_cand, proactive=True, hours=11.0))
    p6_user_turn = uuid.uuid4()
    p6_user_cand = uuid.uuid4()
    rows_candidates.append(candidate_row(p6_user_cand, p6_user_turn, content="reply 6"))
    rows_turns.append(turn_row(p6_user_turn, p6, turn_no=2, author="USER", candidate_id=p6_user_cand, hours=11.3))

    await conn.execute(Chat.__table__.insert(), rows_chats)

    # turns <-> candidates is a circular FK (turns.primary_candidate_id ->
    # candidates.id, candidates.turn_id -> turns.id). Insert turns with a null
    # primary candidate first, then candidates, then back-fill the pointer.
    primary_by_turn = {row["id"]: row.get("primary_candidate_id") for row in rows_turns}
    for row in rows_turns:
        row["primary_candidate_id"] = None
    await conn.execute(Turn.__table__.insert(), rows_turns)
    await conn.execute(Candidate.__table__.insert(), rows_candidates)
    for turn_id, candidate_id in primary_by_turn.items():
        if candidate_id is None:
            continue
        await conn.execute(
            Turn.__table__.update()
            .where(Turn.__table__.c.id == turn_id)
            .values(primary_candidate_id=candidate_id)
        )

    if dispatch_rows:
        await conn.execute(ProactiveMessageDispatch.__table__.insert(), dispatch_rows)


@pytest.mark.asyncio
async def test_continuation_rate_seeded(seeded_conn: AsyncConnection) -> None:
    result = await continuation_rate(
        seeded_conn, window_start=W_START, window_end=W_END, lookahead_hours=1
    )
    # eligible: reply continued (chat1) + lonely reply (chat2); error reply excluded;
    # proactive excluded; 23.5h reply censored
    assert result.status == "ok"
    assert result.denominator == 2
    assert result.numerator == 1
    assert result.value == 0.5
    assert result.censored == 1




@pytest.mark.asyncio
async def test_proactive_reply_rate_seeded(seeded_conn: AsyncConnection) -> None:
    result = await proactive_reply_rate(
        seeded_conn, window_start=W_START, window_end=W_END, lookahead_hours=1
    )
    # valid: P1 (replied), P2 (not replied), P6 archived (replied); P5 censored
    # excluded: P3 missing dispatch, P4 failed dispatch, plus chat4 (the
    # continuation fixture's dispatch-less proactive chat)
    assert result.status == "ok"
    assert result.denominator == 3
    assert result.numerator == 2
    assert result.censored == 1
    assert result.excluded == 3


