"""Monitoring API — sync_log ve guard istatistikleri."""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


# ── Özet ─────────────────────────────────────────────────────────────────────

@router.get("/summary")
async def get_summary(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> Dict[str, Any]:
    """Her job'ın son durumu + 7 günlük trend."""

    # Son durum — job başına en son kayıt
    rows = (await session.execute(text("""
        WITH ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY job_name ORDER BY started_at DESC) AS rn
            FROM sync_log
        )
        SELECT job_name, status, kayit_sayisi,
               started_at, finished_at,
               EXTRACT(EPOCH FROM (finished_at - started_at))::int AS sure_sn,
               hata_mesaji
        FROM ranked WHERE rn = 1
        ORDER BY job_name
    """))).mappings().all()

    son_durum = [dict(r) for r in rows]

    # 7 günlük trend
    trend_rows = (await session.execute(text("""
        SELECT started_at::date AS gun,
               COUNT(*) FILTER (WHERE status = 'success') AS basarili,
               COUNT(*) FILTER (WHERE status IN ('failed','partial')) AS hatali
        FROM sync_log
        WHERE started_at >= NOW() - INTERVAL '7 days'
        GROUP BY started_at::date
        ORDER BY gun
    """))).mappings().all()

    trend = [dict(r) for r in trend_rows]

    # Toplam istatistik
    total = (await session.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE status='success')  AS toplam_basarili,
            COUNT(*) FILTER (WHERE status='failed')   AS toplam_hatali,
            COUNT(*) FILTER (WHERE status='partial')  AS toplam_kısmi,
            ROUND(AVG(EXTRACT(EPOCH FROM (finished_at - started_at)))::numeric, 1) AS ort_sure_sn
        FROM sync_log
        WHERE started_at >= NOW() - INTERVAL '7 days'
          AND finished_at IS NOT NULL
    """))).mappings().first()

    return {
        "son_durum": son_durum,
        "trend_7_gun": trend,
        "ozet": dict(total) if total else {},
    }


# ── Log satırları ─────────────────────────────────────────────────────────────

@router.get("/logs")
async def get_logs(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    limit: int = Query(default=20, le=100),
    job_name: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """sync_log tablosundan son N kayıt."""

    conditions = ["1=1"]
    params: Dict[str, Any] = {"lim": limit}

    if job_name:
        conditions.append("job_name = :job_name")
        params["job_name"] = job_name
    if status:
        conditions.append("status = :status")
        params["status"] = status

    where = " AND ".join(conditions)
    rows = (await session.execute(text(f"""
        SELECT id, job_name, status, kayit_sayisi,
               started_at, finished_at,
               EXTRACT(EPOCH FROM (finished_at - started_at))::int AS sure_sn,
               hata_mesaji, detay
        FROM sync_log
        WHERE {where}
        ORDER BY started_at DESC
        LIMIT :lim
    """), params)).mappings().all()

    return [dict(r) for r in rows]
