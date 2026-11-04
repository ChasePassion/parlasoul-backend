from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args=settings.database_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)


@event.listens_for(Session, "after_begin")
def _bind_rls_context_after_begin(session: Session, transaction, connection) -> None:
    """Bind request user context to every DB transaction for RLS policies."""
    if connection.dialect.name != "postgresql":
        return
    user_id = str(session.info.get("current_user_id") or "")
    connection.execute(
        text("SELECT set_config('app.current_user_id', :user_id, true)"),
        {"user_id": user_id},
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        session.info["current_user_id"] = ""
        try:
            yield session
        finally:
            await session.close()
