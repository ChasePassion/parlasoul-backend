"""Realtime subsystem startup and shutdown helpers."""

from __future__ import annotations


async def startup_realtime(manager) -> None:
    await manager.startup()


async def shutdown_realtime(manager) -> None:
    await manager.shutdown()
