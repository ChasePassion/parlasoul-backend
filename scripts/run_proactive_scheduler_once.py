from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.jobs.proactive_scheduler import run_proactive_scheduler_once


async def main() -> None:
    inserted = await run_proactive_scheduler_once()
    print(f"proactive scheduler inserted {inserted} dispatch(es)")


if __name__ == "__main__":
    asyncio.run(main())
