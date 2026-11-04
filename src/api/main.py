"""Backward-compatible API entrypoint.

Use `src.main:app` as the canonical entrypoint.
"""

from __future__ import annotations

from src.main import app

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    from src.core.config import settings

    uvicorn.run(
        "src.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.API_RELOAD,
        reload_dirs=["src"],
    )
