"""Primary FastAPI entrypoint."""

from __future__ import annotations

from src.bootstrap import create_app
from src.core.config import settings
from src.core.logging import setup_logging

setup_logging()

app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=settings.API_RELOAD,
        reload_dirs=["src"],
    )
