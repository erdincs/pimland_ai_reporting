"""Admin endpoints for connector management and sync operations."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy import text

from app.connectors.registry import registry
from app.connectors.sync_pipeline import run as sync_run
from app.core.config import settings
from app.db.session import SessionLocal
from app.schemas.connector import SourceListItem, SyncJobResult

router = APIRouter(prefix="/connectors", tags=["connectors"])


@router.get("", response_model=List[SourceListItem])
async def list_sources() -> List[SourceListItem]:
    """List all registered data sources and their last sync status."""
    items = []
    for source_id, cfg in registry.all_configs().items():
        last = await _last_sync(source_id)
        items.append(SourceListItem(
            source_id=cfg.source_id,
            type=cfg.type,
            target_table=cfg.target_table,
            description=cfg.description,
            enabled=cfg.enabled,
            last_sync=last,
        ))
    return items


@router.get("/{source_id}", response_model=SourceListItem)
async def get_source(source_id: str) -> SourceListItem:
    _require_source(source_id)
    cfg = registry.all_configs()[source_id]
    last = await _last_sync(source_id)
    return SourceListItem(
        source_id=cfg.source_id,
        type=cfg.type,
        target_table=cfg.target_table,
        description=cfg.description,
        enabled=cfg.enabled,
        last_sync=last,
    )


@router.post("/{source_id}/sync", response_model=SyncJobResult, status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync(source_id: str, background_tasks: BackgroundTasks) -> SyncJobResult:
    """Trigger a background sync for the given source. Returns immediately with job_id."""
    _require_source(source_id)
    import uuid
    job_id = str(uuid.uuid4())
    background_tasks.add_task(_run_sync, source_id, job_id)
    return SyncJobResult(job_id=job_id, source_id=source_id, status="queued")


@router.get("/{source_id}/sync/{job_id}", response_model=SyncJobResult)
async def get_sync_job(source_id: str, job_id: str) -> SyncJobResult:
    """Poll the status of a sync job."""
    result = await _get_job(job_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return result


@router.get("/{source_id}/history", response_model=List[SyncJobResult])
async def sync_history(source_id: str, limit: int = 20) -> List[SyncJobResult]:
    """Last N sync jobs for a source."""
    _require_source(source_id)
    async with SessionLocal() as session:
        rows = await session.execute(text("""
            SELECT job_id, source_id, status, rows_loaded, error_message,
                   started_at, finished_at, duration_ms
            FROM sync_jobs
            WHERE source_id = :sid
            ORDER BY created_at DESC
            LIMIT :lim
        """), {"sid": source_id, "lim": limit})
        return [SyncJobResult(**dict(r)) for r in rows.mappings()]


@router.post("/{source_id}/health")
async def health_check(source_id: str) -> dict:
    """Test connectivity to the upstream source."""
    _require_source(source_id)
    connector = registry.get(source_id)
    ok = await connector.health_check()
    return {"source_id": source_id, "healthy": ok}


@router.post("/reload")
async def reload_registry() -> dict:
    """Hot-reload connector configs from config/sources/*.yaml."""
    registry.reload()
    return {"reloaded": registry.source_ids()}


# ── helpers ─────────────────────────────────────────────────────────────────

def _require_source(source_id: str) -> None:
    if source_id not in registry.source_ids():
        raise HTTPException(
            status_code=404,
            detail=f"Source '{source_id}' not found. Available: {registry.source_ids()}",
        )


async def _run_sync(source_id: str, job_id: str) -> None:
    await sync_run(source_id, job_id=job_id)


async def _last_sync(source_id: str) -> Optional[SyncJobResult]:
    try:
        async with SessionLocal() as session:
            row = await session.execute(text("""
                SELECT job_id, source_id, status, rows_loaded, error_message,
                       started_at, finished_at, duration_ms
                FROM sync_jobs
                WHERE source_id = :sid
                ORDER BY created_at DESC
                LIMIT 1
            """), {"sid": source_id})
            r = row.mappings().first()
            return SyncJobResult(**dict(r)) if r else None
    except Exception:  # noqa: BLE001
        return None


async def _get_job(job_id: str) -> Optional[SyncJobResult]:
    try:
        async with SessionLocal() as session:
            row = await session.execute(text("""
                SELECT job_id, source_id, status, rows_loaded, error_message,
                       started_at, finished_at, duration_ms
                FROM sync_jobs WHERE job_id = :jid
            """), {"jid": job_id})
            r = row.mappings().first()
            return SyncJobResult(**dict(r)) if r else None
    except Exception:  # noqa: BLE001
        return None
