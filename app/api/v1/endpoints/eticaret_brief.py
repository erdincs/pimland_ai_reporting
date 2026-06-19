"""Eticaret Brief — SKL-01..05 veri API'leri + HTML sunucu."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.api.deps import get_readonly_session
from app.core.logging import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

log = get_logger(__name__)

router = APIRouter(prefix="/eticaret-brief", tags=["Eticaret Brief"])

_STATIC = Path(__file__).resolve().parent.parent.parent.parent / "static" / "briefs"


# ── HTML sayfalar ────────────────────────────────────────────────────────────

def _html(name: str) -> HTMLResponse:
    p = _STATIC / name
    if not p.exists():
        return HTMLResponse(f"<h1>Dosya bulunamadı: {name}</h1>", status_code=404)
    return HTMLResponse(content=p.read_text(encoding="utf-8"))


@router.get("/skl01", response_class=HTMLResponse)
async def skl01_html() -> HTMLResponse:
    return _html("skl01.html")


@router.get("/skl02", response_class=HTMLResponse)
async def skl02_html() -> HTMLResponse:
    return _html("skl02.html")


@router.get("/skl03", response_class=HTMLResponse)
async def skl03_html() -> HTMLResponse:
    return _html("skl03.html")


@router.get("/skl04", response_class=HTMLResponse)
async def skl04_html() -> HTMLResponse:
    return _html("skl04.html")


@router.get("/skl05", response_class=HTMLResponse)
async def skl05_html() -> HTMLResponse:
    return _html("skl05.html")


# ── JSON veri API'leri ────────────────────────────────────────────────────────

@router.get("/skl01/data")
async def skl01_data(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    from app.services.daily_brief.eticaret_kpi import get_skl01_data
    try:
        return await get_skl01_data(session)
    except Exception as exc:
        log.error("eticaret_brief.skl01_failed", error=str(exc))
        return {"error": str(exc), "kanallar": [], "kpis": {}, "tempo": {}, "alerts": [], "priorities": []}


@router.get("/skl02/data")
async def skl02_data(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    from app.services.daily_brief.eticaret_kpi import get_skl02_data
    try:
        return await get_skl02_data(session)
    except Exception as exc:
        log.error("eticaret_brief.skl02_failed", error=str(exc))
        return {"error": str(exc), "kpis": {}, "top5": [], "iade5": [], "katalog": {}}


@router.get("/skl03/data")
async def skl03_data(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    from app.services.daily_brief.eticaret_kpi import get_skl03_data
    try:
        return await get_skl03_data(session)
    except Exception as exc:
        log.error("eticaret_brief.skl03_failed", error=str(exc))
        return {"error": str(exc), "grades": {}, "sezonlar": [], "dun_enrichment": {}}


@router.get("/skl04/data")
async def skl04_data(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    from app.services.daily_brief.eticaret_kpi import get_skl04_data
    try:
        return await get_skl04_data(session)
    except Exception as exc:
        log.error("eticaret_brief.skl04_failed", error=str(exc))
        return {"error": str(exc), "kanallar": [], "has_analytics": False, "analytics": [], "kararlar": []}


@router.get("/skl05/data")
async def skl05_data(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    from app.services.daily_brief.eticaret_kpi import get_skl05_data
    try:
        return await get_skl05_data(session)
    except Exception as exc:
        log.error("eticaret_brief.skl05_failed", error=str(exc))
        return {"error": str(exc), "sys_status": "unknown", "pipeline": [], "pim": {}, "enrichment": {}, "siralama": {}}
