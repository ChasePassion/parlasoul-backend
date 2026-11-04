"""Migrate legacy MiniMax system-voice bindings to Bailian (DashScope).

The voice catalog was originally exposed with MiniMax voice ids while the
underlying timbre selection came from the Qwen-TTS catalog.  The two catalogs
share the same 19 characters and a 1:1 id mapping, so a system-voice binding
can move to DashScope without changing the voice the user picked.

Run without ``--apply`` first to inspect the planned changes:

    python scripts/migrate_minimax_system_voices.py
    python scripts/migrate_minimax_system_voices.py --apply

Clone voices are intentionally left untouched: they are account-scoped and are
not portable to another Bailian workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import asyncpg

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.config import settings  # noqa: E402
from src.core.tts_voice_catalog import (  # noqa: E402
    DEFAULT_DASHSCOPE_TTS_MODEL,
    legacy_dashscope_voice_id_for_minimax,
    minimax_system_voice_catalog,
)


def _postgres_dsn() -> str:
    dsn = settings.DATABASE_URL.strip()
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


def _mapping() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in minimax_system_voice_catalog():
        mapped = legacy_dashscope_voice_id_for_minimax(item.provider_voice_id)
        if mapped:
            mapping[item.provider_voice_id] = mapped
    return mapping


async def _run(*, apply: bool, voice_model: str) -> int:
    mapping = _mapping()
    conn = await asyncpg.connect(_postgres_dsn())
    try:
        rows = await conn.fetch(
            """
            select id, name, voice_provider, voice_model, voice_provider_voice_id,
                   voice_source_type
            from characters
            where voice_source_type = 'system'
            order by created_at asc
            """
        )
        planned: list[tuple[asyncpg.Record, str]] = []
        skipped_clone = 0
        already = 0
        unknown: list[asyncpg.Record] = []
        for row in rows:
            if row["voice_provider"] == "dashscope":
                already += 1
                continue
            if row["voice_provider"] != "minimax":
                unknown.append(row)
                continue
            mapped = mapping.get(row["voice_provider_voice_id"])
            if mapped is None:
                unknown.append(row)
                continue
            planned.append((row, mapped))

        clones = await conn.fetchval(
            "select count(*) from characters where voice_source_type = 'clone'"
        )
        skipped_clone = int(clones or 0)

        print(f"system rows scanned: {len(rows)}")
        print(f"already DashScope: {already}")
        print(f"planned migrations: {len(planned)}")
        print(f"clone rows left untouched: {skipped_clone}")
        print(f"unmapped rows: {len(unknown)}")
        for row, mapped in planned:
            print(
                f"  {row['name']}: {row['voice_provider_voice_id']} -> {mapped}"
            )
        for row in unknown:
            print(
                f"  UNMAPPED {row['name']}: "
                f"{row['voice_provider']}/{row['voice_provider_voice_id']}"
            )

        if not apply:
            print("dry run; pass --apply to write")
            return 0

        async with conn.transaction():
            for row, mapped in planned:
                await conn.execute(
                    """
                    update characters
                    set voice_provider = 'dashscope',
                        voice_model = $2,
                        voice_provider_voice_id = $3
                    where id = $1
                    """,
                    row["id"],
                    voice_model,
                    mapped,
                )
        print(f"updated {len(planned)} rows")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the migration")
    parser.add_argument(
        "--voice-model",
        default=settings.DASHSCOPE_TTS_MODEL or DEFAULT_DASHSCOPE_TTS_MODEL,
        help="DashScope TTS model stored on migrated bindings",
    )
    args = parser.parse_args()
    return asyncio.run(_run(apply=args.apply, voice_model=args.voice_model))


if __name__ == "__main__":
    raise SystemExit(main())
