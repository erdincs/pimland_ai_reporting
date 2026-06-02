"""Report generation endpoints — returns live HTML reports."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session
from app.reports.data_queries import collect
from app.reports.ecommerce_monthly import render

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/ecommerce-monthly", response_class=HTMLResponse)
async def ecommerce_monthly_report(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: int = Query(default=2026, ge=2020, le=2030, description="Yıl"),
    ay: int = Query(default=4, ge=1, le=12, description="Ay (1-12)"),
) -> HTMLResponse:
    """E-ticaret aylık satış raporu — Incorta canlı verisi ile HTML."""
    data = await collect(session, yil=yil, ay=ay)
    html = render(data)
    return HTMLResponse(content=html)
