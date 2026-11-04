"""Switch default system TTS voices to MiniMax.

Revision ID: 20260509_0035
Revises: 20260501_0034
Create Date: 2026-05-09 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260509_0035"
down_revision = "20260501_0034"
branch_labels = None
depends_on = None


MINIMAX_MODEL = "speech-2.8-hd"
MINIMAX_DEFAULT_VOICE = "Chinese (Mandarin)_Wise_Women"
DASHSCOPE_MODEL = "qwen3-tts-instruct-flash-realtime"
DASHSCOPE_DEFAULT_VOICE = "Cherry"

DASHSCOPE_TO_MINIMAX = {
    "Cherry": "Chinese (Mandarin)_Wise_Women",
    "Serena": "Chinese (Mandarin)_Gentle_Senior",
    "Ethan": "badao_shaoye",
    "Chelsie": "female-yujie",
    "Momo": "Arrogant_Miss",
    "Vivian": "Attractive_Girl",
    "Moon": "Chinese (Mandarin)_Gentleman",
    "Maia": "female-chengshu",
    "Kai": "bingjiao_didi",
    "Nofish": "Chinese (Mandarin)_Gentle_Youth",
    "Bella": "Charming_Lady",
    "Eldric Sage": "Santa_Claus",
    "Mia": "female-shaonv",
    "Vincent": "Arnold",
    "Bunny": "Chinese (Mandarin)_Cute_Spirit",
    "Elias": "Sweet_Girl",
    "Nini": "Chinese (Mandarin)_Sweet_Lady",
    "Seren": "Chinese (Mandarin)_Mature_Woman",
    "Stella": "Chinese (Mandarin)_Soft_Girl",
}


def _case(mapping: dict[str, str], *, fallback: str) -> str:
    arms = " ".join(
        f"WHEN {sa.literal(old).compile(compile_kwargs={'literal_binds': True})} "
        f"THEN {sa.literal(new).compile(compile_kwargs={'literal_binds': True})}"
        for old, new in mapping.items()
    )
    fallback_sql = sa.literal(fallback).compile(compile_kwargs={"literal_binds": True})
    return f"CASE voice_provider_voice_id {arms} ELSE {fallback_sql} END"


def upgrade() -> None:
    op.alter_column(
        "characters",
        "voice_provider",
        server_default=sa.text("'minimax'"),
        existing_type=sa.String(length=40),
        existing_nullable=False,
    )
    op.alter_column(
        "characters",
        "voice_model",
        server_default=sa.text(f"'{MINIMAX_MODEL}'"),
        existing_type=sa.String(length=120),
        existing_nullable=False,
    )
    op.alter_column(
        "characters",
        "voice_provider_voice_id",
        server_default=sa.text(f"'{MINIMAX_DEFAULT_VOICE}'"),
        existing_type=sa.String(length=191),
        existing_nullable=False,
    )
    op.execute(
        "UPDATE characters "
        "SET voice_provider = 'minimax', "
        f"voice_model = '{MINIMAX_MODEL}', "
        f"voice_provider_voice_id = {_case(DASHSCOPE_TO_MINIMAX, fallback=MINIMAX_DEFAULT_VOICE)} "
        "WHERE voice_source_type = 'system' AND voice_provider = 'dashscope'"
    )


def downgrade() -> None:
    minimax_to_dashscope = {value: key for key, value in DASHSCOPE_TO_MINIMAX.items()}
    op.execute(
        "UPDATE characters "
        "SET voice_provider = 'dashscope', "
        f"voice_model = '{DASHSCOPE_MODEL}', "
        f"voice_provider_voice_id = {_case(minimax_to_dashscope, fallback=DASHSCOPE_DEFAULT_VOICE)} "
        "WHERE voice_source_type = 'system' AND voice_provider = 'minimax'"
    )
    op.alter_column(
        "characters",
        "voice_provider_voice_id",
        server_default=sa.text(f"'{DASHSCOPE_DEFAULT_VOICE}'"),
        existing_type=sa.String(length=191),
        existing_nullable=False,
    )
    op.alter_column(
        "characters",
        "voice_model",
        server_default=sa.text(f"'{DASHSCOPE_MODEL}'"),
        existing_type=sa.String(length=120),
        existing_nullable=False,
    )
    op.alter_column(
        "characters",
        "voice_provider",
        server_default=sa.text("'dashscope'"),
        existing_type=sa.String(length=40),
        existing_nullable=False,
    )
