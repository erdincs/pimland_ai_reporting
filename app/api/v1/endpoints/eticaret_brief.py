"""Eticaret Brief — SKL-01..05 veri API'leri + HTML sunucu."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.api.deps import get_readonly_session
from sqlalchemy.ext.asyncio import AsyncSession

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
    return await get_skl01_data(session)


@router.get("/skl02/data")
async def skl02_data(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    from app.services.daily_brief.eticaret_kpi import get_skl02_data
    return await get_skl02_data(session)


@router.get("/skl03/data")
async def skl03_data(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    from app.services.daily_brief.eticaret_kpi import get_skl03_data
    return await get_skl03_data(session)


@router.get("/skl04/data")
async def skl04_data(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    from app.services.daily_brief.eticaret_kpi import get_skl04_data
    return await get_skl04_data(session)


@router.get("/skl05/data")
async def skl05_data(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    from app.services.daily_brief.eticaret_kpi import get_skl05_data
    return await get_skl05_data(session)
