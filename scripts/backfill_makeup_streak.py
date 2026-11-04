"""
Backfill streak for users with makeup-signed days.

After the logic change that counts makeup days toward the consecutive streak,
this script recalibrates current_natural_streak, longest_natural_streak,
last_natural_signin_date, and last_rewarded_natural_streak based on actual
daily records.

Backfill does not compensate missed historical rewards; it only prevents
duplicate future rewards by capping last_rewarded_natural_streak.

Usage:
    Dry run:  conda run -n parlasoul python scripts/backfill_makeup_streak.py
    Apply:    conda run -n parlasoul python scripts/backfill_makeup_streak.py --apply
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date

from sqlalchemy import text

from src.db.session import engine
from src.services.growth_rules import compute_streak_from_signed_dates


async def backfill(*, dry_run: bool = True) -> None:
    async with engine.connect() as conn:
        # ── tx ──
        async with conn.begin():

            # 1. Find users with makeup-signed days
            result = await conn.execute(text("""
                SELECT DISTINCT user_id FROM growth_daily_stats
                WHERE is_makeup_signed = true
            """))
            user_ids = [row[0] for row in result.fetchall()]

            if not user_ids:
                print("No users with makeup-signed days found.")
                return

            print(f"Found {len(user_ids)} user(s) with makeup-signed days.\n")

            for user_id in user_ids:
                # 2. Fetch current user_stats (only lock in apply mode)
                lock_clause = "" if dry_run else " FOR UPDATE"
                result = await conn.execute(text(f"""
                    SELECT current_natural_streak,
                           longest_natural_streak,
                           last_natural_signin_date,
                           last_rewarded_natural_streak
                    FROM growth_user_stats
                    WHERE user_id = :uid{lock_clause}
                """), {"uid": user_id})
                row = result.fetchone()
                if not row:
                    continue
                old_current, old_longest, last_natural, old_rewarded = row

                # 3. Find all signed dates (natural OR makeup) — daily records
                #    are the single source of truth.
                result = await conn.execute(text("""
                    SELECT stat_date
                    FROM growth_daily_stats
                    WHERE user_id = :uid
                      AND (is_natural_signed = true OR is_makeup_signed = true)
                    ORDER BY stat_date ASC
                """), {"uid": user_id})
                rows = result.fetchall()
                if not rows:
                    continue

                signed_dates: set[date] = {r.stat_date for r in rows}

                # Daily records are the truth; warn if user_stats disagrees.
                anchor = max(signed_dates)
                if last_natural is not None and last_natural > anchor:
                    print(f"  [WARN] user {user_id}: "
                          f"last_natural {last_natural} > max signed date {anchor}")

                effective_anchor = anchor

                # 4. Recalculate streak
                new_streak = compute_streak_from_signed_dates(signed_dates, effective_anchor)
                if new_streak == 0:
                    continue

                new_longest = max(old_longest or 0, new_streak)
                # Cap last_rewarded at nearest 3-day milestone to avoid
                # duplicate makeup-card grants at already-crossed thresholds.
                new_last_rewarded = max(
                    old_rewarded or 0,
                    (new_streak // 3) * 3,
                )

                # 5. Check if anything changed
                changed = (
                    (old_current or 0) != new_streak
                    or (old_longest or 0) != new_longest
                    or (old_rewarded or 0) != new_last_rewarded
                    or last_natural != effective_anchor
                )

                if not changed:
                    print(f"  user {user_id}: no change (streak={new_streak})")
                    continue

                if (old_current or 0) > new_streak:
                    print(f"  [WARN] user {user_id}: "
                          f"current streak decrease {old_current} -> {new_streak}")

                print(f"  user {user_id}:")
                print(f"    current_natural_streak:       {old_current or 0} -> {new_streak}")
                print(f"    longest_natural_streak:       {old_longest or 0} -> {new_longest}")
                print(f"    last_natural_signin_date:     {last_natural} -> {effective_anchor}")
                print(f"    last_rewarded_natural_streak: {old_rewarded or 0} -> {new_last_rewarded}")

                if not dry_run:
                    # Optimistic locking: verify no concurrent modification.
                    result = await conn.execute(text("""
                        UPDATE growth_user_stats
                        SET current_natural_streak       = :curr,
                            longest_natural_streak       = :long,
                            last_natural_signin_date     = :lnsd,
                            last_rewarded_natural_streak = :lrn
                        WHERE user_id = :uid
                          AND current_natural_streak       IS NOT DISTINCT FROM :old_curr
                          AND longest_natural_streak       IS NOT DISTINCT FROM :old_long
                          AND last_natural_signin_date     IS NOT DISTINCT FROM :old_lnsd
                          AND last_rewarded_natural_streak IS NOT DISTINCT FROM :old_lrn
                    """), {
                        "uid": user_id,
                        "curr": new_streak,
                        "long": new_longest,
                        "lnsd": effective_anchor,
                        "lrn": new_last_rewarded,
                        "old_curr": old_current,
                        "old_long": old_longest,
                        "old_lnsd": last_natural,
                        "old_lrn": old_rewarded,
                    })
                    if result.rowcount == 0:
                        print(f"    -> SKIPPED (concurrent modification detected)")
                    else:
                        print(f"    -> UPDATED")

        if dry_run:
            print("\n*** DRY RUN — no changes committed.  Re-run with --apply to persist. ***")
        else:
            print("\n*** Done.  Changes committed. ***")


if __name__ == "__main__":
    dry = "--apply" not in sys.argv
    asyncio.run(backfill(dry_run=dry))
