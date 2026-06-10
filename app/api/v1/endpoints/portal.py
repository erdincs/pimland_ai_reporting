"""Analytics portal endpoints — JSON APIs + HTML shell."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_readonly_session
from app.reports import portal_queries as q
from app.schemas.portal import (
    ColorRow, FilterOptions, KpiSummary, OverviewData,
    ProductDrilldown, ProductList,
)

router = APIRouter(prefix="/portal", tags=["portal"])

_STATIC = Path(__file__).parent.parent.parent.parent / "static"


# ── HTML shell ────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def portal_html() -> HTMLResponse:
    """Serve the analytics portal single-page app."""
    html_path = _STATIC / "portal.html"
    if not html_path.exists():
        raise HTTPException(status_code=503, detail="Portal HTML not built yet.")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@router.get("/yonetim/daily-brief", response_class=HTMLResponse)
async def daily_brief_html() -> HTMLResponse:
    """Günlük Brief sayfası."""
    p = _STATIC / "daily_brief_ekran.html"
    return HTMLResponse(content=p.read_text(encoding="utf-8"))


@router.get("/yonetim/brief-profilleri", response_class=HTMLResponse)
async def brief_profiles_html() -> HTMLResponse:
    """Brief Profil Yönetimi sayfası."""
    p = _STATIC / "daily_brief_profil_yonetim.html"
    return HTMLResponse(content=p.read_text(encoding="utf-8"))


@router.get("/yonetim/soru-havuzu", response_class=HTMLResponse)
async def soru_havuzu_html() -> HTMLResponse:
    """Soru Havuzu Yönetimi — teknik admin ekranı."""
    p = _STATIC / "soru_havuzu.html"
    return HTMLResponse(content=p.read_text(encoding="utf-8"))


@router.get("/search", response_class=HTMLResponse)
async def search_html() -> HTMLResponse:
    """AI Search Engine — ayrı standalone sayfa."""
    html_path = _STATIC / "search.html"
    if not html_path.exists():
        raise HTTPException(status_code=503, detail="Search HTML not found.")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


# ── Data endpoints ────────────────────────────────────────────────────────────

@router.get("/latest-period")
async def latest_period(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> dict:
    """Son tam ay ve yılı döndür — portal açılışında varsayılan dönem."""
    return await q.get_latest_period(session)


@router.get("/filters", response_model=FilterOptions)
async def filters(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
) -> FilterOptions:
    data = await q.get_filters(session)
    return FilterOptions(**data)


@router.get("/kpis", response_model=KpiSummary)
async def kpis(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
) -> KpiSummary:
    data = await q.get_kpis(session, yil=yil, aylar=ay, kanallar=kanal)
    return KpiSummary(**data)


@router.get("/overview", response_model=OverviewData)
async def overview(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
) -> OverviewData:
    data = await q.get_overview(session, yil=yil, aylar=ay, kanallar=kanal)
    return OverviewData(**data)


@router.get("/products", response_model=ProductList)
async def products(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
    sku: Optional[str] = Query(default=None),
    renk: Optional[str] = Query(default=None),
    beden: Optional[str] = Query(default=None),
    marka: Optional[str] = Query(default=None),
    sezon: Optional[str] = Query(default=None),
    urun_grubu: Optional[str] = Query(default=None),
    sort_by: str = Query(default="risk_skoru",
                         pattern="^(risk_skoru|iade_pct|brut_ciro|net_ciro)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=10, le=200),
) -> ProductList:
    data = await q.get_products(
        session, yil=yil, aylar=ay, kanallar=kanal,
        urun_kodu=sku, renk=renk, beden=beden,
        marka=marka, sezon=sezon, urun_grubu=urun_grubu,
        sort_by=sort_by, page=page, page_size=page_size,
    )
    return ProductList(**data)


@router.get("/products/{urun_kodu}", response_model=ProductDrilldown)
async def product_drilldown(
    urun_kodu: str,
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
) -> ProductDrilldown:
    data = await q.get_product_drilldown(
        session, urun_kodu=urun_kodu, yil=yil, aylar=ay
    )
    if not data:
        raise HTTPException(status_code=404, detail=f"SKU '{urun_kodu}' bulunamadı.")
    return ProductDrilldown(**data)


@router.get("/colors")
async def colors(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
) -> list:
    return await q.get_colors(session, yil=yil, aylar=ay, kanallar=kanal)


@router.get("/exec-summary")
async def exec_summary(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
) -> dict:
    return await q.get_exec_summary(session, yil=yil, aylar=ay, kanallar=kanal)


@router.get("/kategori")
async def kategori(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
) -> list:
    return await q.get_kategori(session, yil=yil, aylar=ay, kanallar=kanal)


@router.get("/top-urunler")
async def top_urunler(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
    limit: int = Query(default=10, ge=5, le=50),
    sort_by: str = Query(default="net_ciro", pattern="^(net_ciro|iade_pct|brut_ciro)$"),
) -> list:
    return await q.get_top_urunler(session, yil=yil, aylar=ay, kanallar=kanal,
                                    limit=limit, sort_by=sort_by)


@router.get("/iade-analiz")
async def iade_analiz(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
) -> dict:
    return await q.get_iade_analiz(session, yil=yil, aylar=ay, kanallar=kanal)


@router.get("/karlilik")
async def karlilik(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
) -> dict:
    return await q.get_karlilik(session, yil=yil, aylar=ay, kanallar=kanal)


@router.get("/urun-satis-analiz")
async def urun_satis_analiz(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
    marka: Optional[str] = Query(default=None),
    sezon_kodu: Optional[str] = Query(default=None),
) -> dict:
    return await q.get_urun_satis_analiz(
        session, yil=yil, aylar=ay, kanallar=kanal,
        marka=marka, sezon_kodu=sezon_kodu,
    )


@router.get("/urun-satis-detail")
async def urun_satis_detail(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    marka: str = Query(...),
    sezon_kodu: str = Query(...),
    ana_grup: str = Query(...),
    urun_grubu: str = Query(...),
    yil: Optional[int] = Query(default=None),
    ay: List[int] = Query(default=[]),
    kanal: List[str] = Query(default=[]),
) -> list:
    return await q.get_urun_satis_detail(
        session, marka=marka, sezon_kodu=sezon_kodu,
        ana_grup=ana_grup, urun_grubu=urun_grubu,
        yil=yil, aylar=ay, kanallar=kanal,
    )


@router.get("/urun-yonetimi")
async def urun_yonetimi(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    marka: Optional[str] = Query(default=None),
    sezon: Optional[str] = Query(default=None),
    tema:  Optional[str] = Query(default=None),
) -> dict:
    return await q.get_urun_yonetimi(session, marka=marka, sezon=sezon, tema=tema)


@router.get("/plm-katalog")
async def plm_katalog(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    marka: Optional[str] = Query(default=None),
    sezon: Optional[str] = Query(default=None),
    tema:  Optional[str] = Query(default=None),
) -> dict:
    return await q.get_plm_katalog(session, marka=marka, sezon=sezon, tema=tema)


@router.get("/product-live/{urun_kodu}")
async def product_live(urun_kodu: str) -> dict:
    """SKU için gerçek zamanlı Pimland verisi: stok, fiyat, finansal, performans.

    Pimland MCP'den paralel çeker — 6 tool eş zamanlı.
    Redis cache: 15 dakika TTL.
    """
    from app.services.product_live import get_product_live
    return await get_product_live(urun_kodu)
