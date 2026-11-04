import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db.session import engine  # noqa: E402


async def main() -> None:
    async with engine.connect() as conn:
        cols = await conn.execute(
            text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND (
                    (table_name='chats' AND column_name IN ('last_turn_no','active_leaf_turn_id'))
                    OR (table_name='turns' AND column_name IN ('parent_turn_id','parent_candidate_id'))
                  )
                ORDER BY table_name, column_name
                """
            )
        )
        print("columns:", cols.fetchall())

        cons = await conn.execute(
            text(
                """
                SELECT conname
                FROM pg_constraint
                WHERE conname IN ('turns_chat_turn_no_uniq','candidates_turn_candidate_no_uniq')
                ORDER BY conname
                """
            )
        )
        print("constraints:", [r[0] for r in cons.fetchall()])

        idx = await conn.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname='public'
                  AND indexname IN ('turns_chat_parent_turn_idx','turns_parent_candidate_uniq')
                ORDER BY indexname
                """
            )
        )
        print("indexes:", [r[0] for r in idx.fetchall()])

        # Quick sanity stats
        stats = await conn.execute(
            text(
                """
                SELECT
                  (SELECT COUNT(*) FROM chats) AS chats_total,
                  (SELECT COUNT(*) FROM chats WHERE active_leaf_turn_id IS NOT NULL) AS chats_with_leaf,
                  (SELECT COUNT(*) FROM turns) AS turns_total,
                  (SELECT COUNT(*) FROM turns WHERE parent_turn_id IS NOT NULL) AS turns_with_parent
                """
            )
        )
        print("stats:", dict(stats.mappings().first()))

        ident = await conn.execute(
            text(
                """
                SELECT table_name, column_name, is_identity, identity_generation
                FROM information_schema.columns
                WHERE table_schema='public'
                  AND (
                    (table_name='turns' AND column_name='turn_no')
                    OR (table_name='candidates' AND column_name='candidate_no')
                  )
                ORDER BY table_name, column_name
                """
            )
        )
        print("identity:", [dict(r) for r in ident.mappings().all()])

        bad = await conn.execute(
            text(
                """
                SELECT conname
                FROM pg_constraint
                WHERE conname IN ('turns_turn_no_key','candidates_candidate_no_key')
                ORDER BY conname
                """
            )
        )
        bad_names = [r[0] for r in bad.fetchall()]
        print("legacy_global_uniques_present:", bad_names)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
