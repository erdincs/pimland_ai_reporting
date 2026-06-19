"""adL Premium Brief v2 — endpoint'ler."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.services.brief_ec_service import generate_ec_brief
from app.services.brief_mg_service import generate_mg_brief

router = APIRouter(prefix="/brief", tags=["Brief v2"])


async def _cached_html(session: AsyncSession, brief_type: str, gun: date) -> Optional[str]:
    """brief_history'de kayıt varsa HTML'i döner; yoksa None."""
    row = (await session.execute(
        text("""
            SELECT html_content FROM brief_history
            WHERE brief_type = :tip AND brief_date = :gun
              AND status = 'ok' AND html_content IS NOT NULL
            ORDER BY generated_at DESC LIMIT 1
        """),
        {"tip": brief_type, "gun": gun},
    )).one_or_none()
    return row[0] if row else None


@router.get(
    "/ec",
    response_class=HTMLResponse,
    summary="adL E-Ticaret Günlük Brief",
    description=(
        "Belirtilen tarihe ait e-ticaret brief'ini üretir ve HTML döndürür. "
        "`gun` girilmezse dünün tarihi kullanılır."
    ),
)
async def ec_brief(
    session: Annotated[AsyncSession, Depends(get_session)],
    gun: date = Query(
        default=None,
        description="Brief tarihi YYYY-MM-DD. Varsayılan: dün.",
        example="2026-06-16",
    ),
    force: bool = Query(default=False, description="True → önbelleği atla, yeniden üret."),
    period: int = Query(default=7, ge=7, le=30, description="Trend penceresi (7/15/30 gün)."),
) -> HTMLResponse:
    if gun is None:
        gun = date.today() - timedelta(days=1)

    if not force:
        cached = await _cached_html(session, "EC", gun)
        if cached:
            return HTMLResponse(content=cached, status_code=200)

    result = await generate_ec_brief(session, gun, period=period)

    if result["status"] == "no_data":
        html = _no_data_html("adL E-Ticaret Brief", result.get("message", "Veri bulunamadı."))
        return HTMLResponse(content=html, status_code=200)

    return HTMLResponse(content=result["html"], status_code=200)


@router.get(
    "/ec/json",
    summary="adL E-Ticaret Brief — JSON meta",
    description="Brief'i üretir; HTML yerine meta bilgisi (durum, süre, tarih) döndürür.",
)
async def ec_brief_json(
    session: Annotated[AsyncSession, Depends(get_session)],
    gun: Optional[date] = Query(default=None, description="Brief tarihi YYYY-MM-DD."),
) -> JSONResponse:
    if gun is None:
        gun = date.today() - timedelta(days=1)

    result = await generate_ec_brief(session, gun)
    payload = {k: v for k, v in result.items() if k != "html"}
    if result.get("html"):
        payload["html_len"] = len(result["html"])
    return JSONResponse(content=payload)


def _no_data_html(title: str, message: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Veri Yok</title></head>"
        "<body style='background:#0d0d12;color:#f0f0f8;font-family:sans-serif;"
        "padding:40px;max-width:680px;margin:auto'>"
        f"<h2 style='color:#c9a961'>{title}</h2>"
        f"<p>{message}</p>"
        "</body></html>"
    )


@router.get(
    "/mg",
    response_class=HTMLResponse,
    summary="adL Mağaza Günlük Brief",
    description=(
        "Belirtilen tarihe ait mağaza brief'ini üretir ve HTML döndürür. "
        "`gun` girilmezse dünün tarihi kullanılır."
    ),
)
async def mg_brief(
    session: Annotated[AsyncSession, Depends(get_session)],
    gun: Optional[date] = Query(
        default=None,
        description="Brief tarihi YYYY-MM-DD. Varsayılan: dün.",
        example="2026-06-16",
    ),
    force: bool = Query(default=False, description="True → önbelleği atla, yeniden üret."),
) -> HTMLResponse:
    if gun is None:
        gun = date.today() - timedelta(days=1)

    if not force:
        cached = await _cached_html(session, "MG", gun)
        if cached:
            return HTMLResponse(content=cached, status_code=200)

    result = await generate_mg_brief(session, gun)

    if result["status"] == "no_data":
        html = _no_data_html("adL Mağaza Brief", result.get("message", "Veri bulunamadı."))
        return HTMLResponse(content=html, status_code=200)

    return HTMLResponse(content=result["html"], status_code=200)


@router.get(
    "/mg/json",
    summary="adL Mağaza Brief — JSON meta",
    description="Brief'i üretir; HTML yerine meta bilgisi döndürür.",
)
async def mg_brief_json(
    session: Annotated[AsyncSession, Depends(get_session)],
    gun: Optional[date] = Query(default=None, description="Brief tarihi YYYY-MM-DD."),
) -> JSONResponse:
    if gun is None:
        gun = date.today() - timedelta(days=1)

    result = await generate_mg_brief(session, gun)
    payload = {k: v for k, v in result.items() if k != "html"}
    if result.get("html"):
        payload["html_len"] = len(result["html"])
    return JSONResponse(content=payload)
