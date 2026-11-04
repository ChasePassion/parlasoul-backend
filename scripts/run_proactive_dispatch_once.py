from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.session import AsyncSessionLocal
from src.jobs.proactive_scheduler import process_one_proactive_dispatch
from src.repositories.proactive_dispatch_repository import ProactiveDispatchRepository


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Insert and/or process one proactive dispatch")
    parser.add_argument("--user-id", type=uuid.UUID, help="Optional user id to enqueue first")
    parser.add_argument(
        "--slot-at",
        type=str,
        default=None,
        help="ISO datetime with timezone, defaults to now UTC when --user-id is provided",
    )
    parser.add_argument("--slot-label", type=str, default="manual")
    parser.add_argument("--timezone", type=str, default="UTC")
    return parser.parse_args()


def _parse_slot_at(raw_value: str | None) -> datetime:
    if not raw_value:
        return datetime.now(UTC)
    value = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _enqueue_if_requested(args: argparse.Namespace) -> None:
    if args.user_id is None:
        return
    async with AsyncSessionLocal() as db:
        db.info["current_user_id"] = "system"
        repo = ProactiveDispatchRepository(db)
        created = await repo.enqueue_unique(
            user_id=args.user_id,
            slot_at=_parse_slot_at(args.slot_at),
            slot_label=args.slot_label,
            timezone=args.timezone,
            payload_json={"manual_script": True},
        )
        await db.commit()
    print("dispatch enqueued" if created else "dispatch already existed")


async def main() -> None:
    args = _parse_args()
    await _enqueue_if_requested(args)
    processed = await process_one_proactive_dispatch()
    print("processed one proactive dispatch" if processed else "no due proactive dispatch found")


if __name__ == "__main__":
    asyncio.run(main())
