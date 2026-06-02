"""Liveness & readiness probes."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.db.session import ro_engine
from app.services import cache_service

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: process is up."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, object]:
    """Readiness: dependencies (DB, Redis) are reachable."""
    db_ok = False
    try:
        async with ro_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        db_ok = False

    redis_ok = await cache_service.ping()
    return {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "database": db_ok,
        "redis": redis_ok,
    }
