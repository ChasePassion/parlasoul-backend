#!/usr/bin/env python3
"""Prune evaluation runs past their retention window (system context only).

Deletes eval_runs (results cascade). Logs run ids and counts only — never
raw text.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.core.config import settings  # noqa: E402
from src.db.models import EvalRun  # noqa: E402
from src.evaluation.storage.db_writer import DbWriter  # noqa: E402


async def prune(*, retention_days: int, dry_run: bool) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    writer = DbWriter()
    async with writer._system_session() as session:  # noqa: SLF001 - system context
        rows = await session.execute(
            select(EvalRun.id, EvalRun.created_at, EvalRun.retention_until).where(
                EvalRun.created_at < cutoff
            )
        )
        expired = rows.all()
        # retention_until override: keep runs whose retention extends further
        now = datetime.now(UTC)
        to_delete = [
            run_id
            for run_id, created_at, retention_until in expired
            if retention_until is None or retention_until <= now
        ]

    print(f"prune: cutoff={cutoff.isoformat()} expired_runs={len(expired)} deletable={len(to_delete)}")
    if dry_run:
        for run_id in to_delete:
            print(f"prune: [dry-run] would delete run {run_id}")
        return 0

    deleted = 0
    for run_id in to_delete:
        await writer.delete_run(run_id)
        deleted += 1
        print(f"prune: deleted run {run_id}")
    print(f"prune: done, deleted={deleted}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Prune evaluation runs past retention.")
    parser.add_argument("--retention-days", type=int, default=settings.EVAL_RETENTION_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.retention_days <= 0:
        print("prune: retention-days must be positive", file=sys.stderr)
        return 2
    return asyncio.run(prune(retention_days=args.retention_days, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
