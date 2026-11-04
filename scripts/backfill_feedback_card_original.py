"""Backfill display_surface and source_meta for existing feedback_card saved items.

Before this fix, feedback_card saved items stored the "better expression" as
display_surface instead of the user's original sentence.  This script looks up
the original sentence from the turn's primary candidate and rewrites the record.

Handles duplicate collisions: if multiple feedback_card items for the same user
would end up with the same original sentence as display_surface, only the latest
one is kept and the rest are deleted.
"""

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db.session import engine  # noqa: E402


async def main() -> None:
    async with engine.begin() as conn:
        # Step 1: find items that would collide after update (same user + same original)
        dupes = await conn.execute(
            text("""
                SELECT si.user_id, cand.content AS original, array_agg(si.id ORDER BY si.created_at DESC) AS ids
                FROM saved_items si
                JOIN turns t ON t.id = si.source_turn_id
                JOIN candidates cand ON cand.id = t.primary_candidate_id
                WHERE si.kind = 'feedback_card'
                  AND si.display_surface IS DISTINCT FROM cand.content
                GROUP BY si.user_id, cand.content
                HAVING count(*) > 1
            """)
        )
        dupe_rows = dupes.fetchall()
        ids_to_delete: list[str] = []
        for row in dupe_rows:
            # Keep the first (most recent), delete the rest
            ids_to_delete.extend(str(rid) for rid in row[2][1:])

        if ids_to_delete:
            await conn.execute(
                text("DELETE FROM saved_items WHERE id = ANY(:ids)"),
                {"ids": ids_to_delete},
            )
            print(f"Deleted {len(ids_to_delete)} duplicate feedback_card items.")

        # Step 2: update remaining items
        result = await conn.execute(
            text("""
                WITH to_fix AS (
                    SELECT si.id, cand.content AS original
                    FROM saved_items si
                    JOIN turns t ON t.id = si.source_turn_id
                    JOIN candidates cand ON cand.id = t.primary_candidate_id
                    WHERE si.kind = 'feedback_card'
                      AND si.display_surface IS DISTINCT FROM cand.content
                )
                UPDATE saved_items si
                SET
                    display_surface = tf.original,
                    source_meta = CASE
                        WHEN jsonb_typeof(COALESCE(si.source_meta, '{}'::jsonb)) = 'object'
                        THEN COALESCE(si.source_meta, '{}'::jsonb)
                             || jsonb_build_object('original_content', tf.original)
                        ELSE jsonb_build_object('original_content', tf.original)
                    END
                FROM to_fix tf
                WHERE si.id = tf.id
                RETURNING si.id
            """)
        )
        updated_ids = [row[0] for row in result.fetchall()]
        print(f"Backfilled {len(updated_ids)} feedback_card saved items.")
        for uid in updated_ids:
            print(f"  {uid}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
