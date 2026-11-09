"""FastAPI app factory and lifecycle wiring."""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from src.api.exceptions import register_exception_handlers
from src.api.response import success_response
from src.api.routers import (
    auth,
    character_forge,
    characters,
    chats,
    discover,
    growth,
    learning,
    memories,
    payments,
    realtime,
    realtime_telemetry,
    saved_items,
    turns,
    upload,
    users,
    voice,
    voices,
    webhooks,
)
from src.core.config import settings
from src.core.dependencies import get_memory_system, get_realtime_session_manager
from src.core.logging import shutdown_logging
from src.core.logging_context import clear_request_user_id, reset_tokens, set_request_context
from src.db.schema_guard import assert_required_columns
from src.db.session import engine
from src.jobs.proactive_scheduler import proactive_scheduler_loop, proactive_worker_loop
from src.jobs.semantic_scheduler import semantic_consolidation_scheduler_loop
from src.observability import init_langfuse
from src.realtime.lifespan import shutdown_realtime, startup_realtime
from src.services.media_sync_worker import get_media_sync_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting ParlaSoul API server...")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            if settings.DB_SCHEMA_COMPAT_CHECK_ENABLED:
                await assert_required_columns(conn)
    except Exception as exc:
        logger.error("Database startup check failed: %s", exc)
        raise

    init_langfuse()

    # Warm up memory subsystem to reduce first-request 503 spikes when Milvus has
    # transient startup/network jitter. If warm-up still fails, keep app up so
    # non-memory endpoints remain available.
    try:
        await asyncio.wait_for(
            asyncio.to_thread(get_memory_system),
            timeout=settings.MEMORY_WARMUP_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Memory warm-up failed at startup; memory endpoints may return 503: %s", exc)

    if settings.ENABLE_SEMANTIC_CONSOLIDATION_JOB:
        app.state.semantic_consolidation_task = asyncio.create_task(
            semantic_consolidation_scheduler_loop(),
            name="semantic_consolidation_scheduler",
        )
    else:
        app.state.semantic_consolidation_task = None
        logger.info("Semantic consolidation scheduler disabled by configuration.")

    if settings.ENABLE_PROACTIVE_MESSAGE_JOB:
        app.state.proactive_scheduler_task = asyncio.create_task(
            proactive_scheduler_loop(),
            name="proactive_scheduler",
        )
        app.state.proactive_worker_tasks = [
            asyncio.create_task(
                proactive_worker_loop(worker_name=f"proactive_worker_{index + 1}"),
                name=f"proactive_worker_{index + 1}",
            )
            for index in range(settings.PROACTIVE_JOB_WORKER_COUNT)
        ]
    else:
        app.state.proactive_scheduler_task = None
        app.state.proactive_worker_tasks = []
        logger.info("Proactive message scheduler disabled by configuration.")

    app.state.realtime_session_manager = get_realtime_session_manager()
    await startup_realtime(app.state.realtime_session_manager)

    # Uploads are served from local disk; this worker mirrors them into R2.
    media_sync_worker = get_media_sync_worker()
    await media_sync_worker.start()
    app.state.media_sync_worker = media_sync_worker

    yield

    logger.info("Shutting down ParlaSoul API server...")
    await shutdown_realtime(app.state.realtime_session_manager)
    await media_sync_worker.stop()
    task = getattr(app.state, "semantic_consolidation_task", None)
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    proactive_scheduler_task = getattr(app.state, "proactive_scheduler_task", None)
    if proactive_scheduler_task:
        proactive_scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await proactive_scheduler_task
    proactive_worker_tasks = getattr(app.state, "proactive_worker_tasks", [])
    if proactive_worker_tasks:
        for task in proactive_worker_tasks:
            task.cancel()
        for task in proactive_worker_tasks:
            with suppress(asyncio.CancelledError):
                await task
    await engine.dispose()
    shutdown_logging()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        openapi_url=f"{settings.API_V1_STR}/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context_middleware(request, call_next):
        request_id = (
            request.headers.get("x-request-id")
            or request.headers.get("x-correlation-id")
            or request.headers.get("x-amzn-trace-id")
            or str(uuid.uuid4())
        )

        tokens = set_request_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            clear_request_user_id()
            reset_tokens(tokens)

        response.headers["X-Request-Id"] = request_id
        return response

    register_exception_handlers(app)

    app.include_router(webhooks.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(payments.router)
    app.include_router(upload.router)
    app.include_router(memories.router)
    app.include_router(characters.router)
    app.include_router(character_forge.router)
    app.include_router(discover.router)
    app.include_router(growth.router)
    app.include_router(chats.router)
    app.include_router(turns.router)
    app.include_router(learning.router)
    app.include_router(saved_items.router)
    app.include_router(voice.router)
    app.include_router(voices.router)
    app.include_router(realtime.router)
    app.include_router(realtime_telemetry.router)

    @app.get("/health")
    async def health_check():
        return success_response({"status": "ok"}, status_code=200)

    return app
