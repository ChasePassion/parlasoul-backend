import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

# Ensure repo root is on sys.path so `import src...` works when running as a script.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db.session import engine  # noqa: E402


async def _constraint_exists(conn, name: str) -> bool:
    res = await conn.execute(text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name})
    return res.first() is not None


async def main() -> None:
    async with engine.begin() as conn:
        # Columns
        await conn.execute(text("ALTER TABLE chats ADD COLUMN IF NOT EXISTS last_turn_no BIGINT NULL"))
        await conn.execute(text("ALTER TABLE chats ADD COLUMN IF NOT EXISTS active_leaf_turn_id UUID NULL"))

        # Drop legacy global uniqueness that breaks per-chat/per-turn numbering.
        await conn.execute(text("ALTER TABLE turns DROP CONSTRAINT IF EXISTS turns_turn_no_key"))
        await conn.execute(text("ALTER TABLE candidates DROP CONSTRAINT IF EXISTS candidates_candidate_no_key"))

        # Ensure turn_no/candidate_no are application-assigned (not GENERATED ALWAYS identity).
        await conn.execute(text("ALTER TABLE turns ALTER COLUMN turn_no DROP IDENTITY IF EXISTS"))
        await conn.execute(text("ALTER TABLE candidates ALTER COLUMN candidate_no DROP IDENTITY IF EXISTS"))

        # Re-sequence existing rows to dense 1..N numbering within their scope for stable UI.
        await conn.execute(
            text(
                """
                WITH ordered AS (
                  SELECT
                    id,
                    chat_id,
                    ROW_NUMBER() OVER (PARTITION BY chat_id ORDER BY turn_no) AS new_no
                  FROM turns
                )
                UPDATE turns t
                SET turn_no = o.new_no
                FROM ordered o
                WHERE t.id = o.id
                  AND t.turn_no IS DISTINCT FROM o.new_no
                """
            )
        )
        await conn.execute(
            text(
                """
                WITH ordered AS (
                  SELECT
                    id,
                    turn_id,
                    ROW_NUMBER() OVER (PARTITION BY turn_id ORDER BY candidate_no) AS new_no
                  FROM candidates
                )
                UPDATE candidates c
                SET candidate_no = o.new_no
                FROM ordered o
                WHERE c.id = o.id
                  AND c.candidate_no IS DISTINCT FROM o.new_no
                """
            )
        )

        await conn.execute(text("ALTER TABLE turns ADD COLUMN IF NOT EXISTS parent_turn_id UUID NULL"))
        await conn.execute(text("ALTER TABLE turns ADD COLUMN IF NOT EXISTS parent_candidate_id UUID NULL"))

        # Constraints
        if not await _constraint_exists(conn, "turns_chat_turn_no_uniq"):
            await conn.execute(text("ALTER TABLE turns ADD CONSTRAINT turns_chat_turn_no_uniq UNIQUE (chat_id, turn_no)"))

        if not await _constraint_exists(conn, "candidates_turn_candidate_no_uniq"):
            await conn.execute(
                text("ALTER TABLE candidates ADD CONSTRAINT candidates_turn_candidate_no_uniq UNIQUE (turn_id, candidate_no)")
            )

        # Indexes
        await conn.execute(text("CREATE INDEX IF NOT EXISTS turns_chat_parent_turn_idx ON turns (chat_id, parent_turn_id)"))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS turns_parent_candidate_uniq "
                "ON turns (parent_candidate_id) WHERE parent_candidate_id IS NOT NULL"
            )
        )

        # Backfill: chats.last_turn_no
        await conn.execute(
            text(
                """
                UPDATE chats c
                SET last_turn_no = t.max_turn_no
                FROM (
                  SELECT chat_id, MAX(turn_no) AS max_turn_no
                  FROM turns
                  GROUP BY chat_id
                ) t
                WHERE c.id = t.chat_id
                  AND (c.last_turn_no IS NULL OR c.last_turn_no <> t.max_turn_no)
                """
            )
        )

        # Backfill: chats.active_leaf_turn_id
        await conn.execute(
            text(
                """
                UPDATE chats c
                SET active_leaf_turn_id = c.last_turn_id
                WHERE c.active_leaf_turn_id IS NULL
                  AND c.last_turn_id IS NOT NULL
                """
            )
        )
        await conn.execute(
            text(
                """
                UPDATE chats c
                SET active_leaf_turn_id = x.last_turn_id
                FROM (
                  SELECT DISTINCT ON (chat_id) chat_id, id AS last_turn_id
                  FROM turns
                  ORDER BY chat_id, turn_no DESC
                ) x
                WHERE c.id = x.chat_id
                  AND c.active_leaf_turn_id IS NULL
                """
            )
        )

        # Backfill: turns.parent_* from linear turn_no order
        await conn.execute(
            text(
                """
                WITH ordered AS (
                  SELECT
                    t.id AS turn_id,
                    t.chat_id,
                    t.turn_no,
                    LAG(t.id) OVER (PARTITION BY t.chat_id ORDER BY t.turn_no) AS parent_turn_id,
                    LAG(t.primary_candidate_id) OVER (PARTITION BY t.chat_id ORDER BY t.turn_no) AS parent_candidate_id
                  FROM turns t
                )
                UPDATE turns t
                SET parent_turn_id = o.parent_turn_id,
                    parent_candidate_id = o.parent_candidate_id
                FROM ordered o
                WHERE t.id = o.turn_id
                  AND (t.parent_turn_id IS DISTINCT FROM o.parent_turn_id
                       OR t.parent_candidate_id IS DISTINCT FROM o.parent_candidate_id)
                """
            )
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
