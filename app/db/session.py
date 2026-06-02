"""Database engines & session factories.

Two engines on purpose:

* ``engine``       — full-privilege, for app-owned writes (ingestion metadata,
                     future auth tables, materialized-view refreshes).
* ``ro_engine``    — least-privilege role used to EXECUTE LLM-generated SQL.
                     A read-only DB role is the strongest guardrail against a
                     prompt-injected ``DELETE``; the SQL guard is layer two.

Both are async (asyncpg). Alembic and the Excel loader use the *sync* URL.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

# Primary read/write engine.
engine: AsyncEngine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=settings.app_debug,
)

# Read-only engine for executing generated SQL. statement_timeout caps runaway
# queries at the connection level.
ro_engine: AsyncEngine = create_async_engine(
    settings.readonly_database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    echo=False,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
ReadOnlySessionLocal = async_sessionmaker(ro_engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: a read/write session, committed on success."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_readonly_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: a read-only session for executing generated SQL."""
    async with ReadOnlySessionLocal() as session:
        yield session
