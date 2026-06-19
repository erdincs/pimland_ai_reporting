"""Analytics portal endpoints — JSON APIs + HTML shell."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm_client import llm_client
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


@router.get("/eticaret-gunluk")
async def eticaret_gunluk(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    gun_sayisi: int = Query(default=30, ge=1, le=90),
) -> dict:
    return await q.get_eticaret_gunluk(session, gun_sayisi=gun_sayisi)


@router.get("/magaza-gunluk")
async def magaza_gunluk(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    gun_sayisi: int = Query(default=30, ge=1, le=90),
) -> dict:
    return await q.get_magaza_gunluk(session, gun_sayisi=gun_sayisi)


# ── ADL RAPORLAR ──────────────────────────────────────────────────────────────

@router.get("/adl/yonetici")
async def adl_yonetici(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    gun_sayisi: int = Query(default=7, ge=1, le=30),
) -> dict:
    return await q.get_adl_yonetici(session, gun_sayisi=gun_sayisi)


@router.get("/adl/eticaret")
async def adl_eticaret(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    gun_sayisi: int = Query(default=30, ge=1, le=90),
) -> dict:
    return await q.get_adl_eticaret(session, gun_sayisi=gun_sayisi)


@router.get("/adl/magaza")
async def adl_magaza(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    gun_sayisi: int = Query(default=30, ge=1, le=90),
) -> dict:
    return await q.get_adl_magaza(session, gun_sayisi=gun_sayisi)


@router.get("/adl/premium")
async def adl_premium(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    ay_count: int = Query(default=3, ge=1, le=12),
) -> dict:
    return await q.get_adl_premium(session, ay_count=ay_count)


@router.get("/adl/urun-stok")
async def adl_urun_stok(
    session: Annotated[AsyncSession, Depends(get_readonly_session)],
    ay_count: int = Query(default=3, ge=1, le=12),
) -> dict:
    return await q.get_adl_urun_stok(session, ay_count=ay_count)


@router.post("/adl/ai-yorum")
async def adl_ai_yorum(request: Request) -> dict:
    body = await request.json()
    rapor_tipi = body.get("rapor_tipi", "genel")
    ozet_data  = body.get("ozet_data", {})

    _FORMAT = """
Yanıt MUTLAKA şu 3 bölümde olmalı (başlıkları AYNEN yaz):

[ÖZET]
En kritik tek cümle bulgu.

[ANALİZ]
3-5 cümle veri yorumu. Geçmiş dönem karşılaştırması zorunlu. Kesinlik iddia etme — "tahmini / yaklaşık" kullan.

[AKSİYONLAR]
JSON listesi, tam bu şemada:
[{"priority":1,"title":"...","description":"...","expected_impact":"...","responsible":"..."},{"priority":2,...},{"priority":3,...}]

Sayı formatı: 1.234.567 ₺ · %14,7"""

    _SYSTEMS: dict = {
        "yonetici-ozeti": (
            "Sen adL premium kadın moda markası için kıdemli yönetim danışmanısın.\n"
            "Hedef okuyucu: CEO, Genel Müdür. Maksimum 2 dakikada okunmalı.\n"
            "Dil: Türkçe. Ton: yönetici düzeyinde, rakam önce, jargon yok.\n"
            "Her metrik için tanımlayıcı → tanısal → tahminsel → aksiyonel sırayı izle.\n"
            "Premium marka penceresinde: tam fiyat oranı düşüşüne özellikle hassas ol."
            + _FORMAT
        ),
        "eticaret-raporu": (
            "Sen adL premium kadın moda markası için e-ticaret analistisin.\n"
            "Hedef: E-ticaret müdürü ve dijital pazarlama ekibi.\n"
            "Dil: Türkçe. Ton: operasyonel, kanal odaklı, SKU detaylı.\n"
            "ADL sahip kanal — Trendyol %18 komisyon → ADL matematiksel olarak %18 daha karlı.\n"
            "İade paternlerinde ürün × beden × renk kombinasyonlarını ön plana çıkar.\n"
            "GA4 benchmark: conversion >%2 orta, >%4 iyi, >%5 mükemmel; bounce <%40 iyi."
            + _FORMAT
        ),
        "magaza-raporu": (
            "Sen adL premium kadın moda markası için mağaza operasyon analistisin.\n"
            "Hedef: Mağaza operasyon müdürü ve bölge müdürleri.\n"
            "Dil: Türkçe. Ton: operasyonel, mağaza odaklı, bölge müdürlerine yönelik.\n"
            "adL MDO ~%5.91 — sektör normu %12-18 arası. Bu BÜYÜK bir açık, yorumla.\n"
            "%1 MDO artışı ≈ %6.8 ciro artışı (sabit ziyaretçide). Bu denklem zorunlu.\n"
            "Sepet ortalaması ~1.90 — norm 2.5-3.5. Çapraz satış açığını adresle."
            + _FORMAT
        ),
        "premium-marka": (
            "Sen adL premium kadın moda markası için kıdemli marka stratejistisin.\n"
            "Hedef: Brand Manager, Ürün Müdürü, CEO.\n"
            "Dil: Türkçe. Ton: stratejik, uzun vadeli, marka değer odaklı.\n"
            "KURAL: 'tam fiyat ↓ → marka erozyonu' denklemini ASLA göz ardı etme.\n"
            "İndirimi savunma değil, marka aşınması erken uyarı sinyali olarak yorumla.\n"
            "Sektör benchmark: Beymen/Vakko/İpekyol premium norm. Premium+Luxury ciro payı >%50 hedef.\n"
            "TARGET_MIX: ELB %35, TRK %20, DGI %15, PNT %10, BLZ %8, AKS %7. ±10% sapma sinyal."
            + _FORMAT
        ),
        "urun-stok": (
            "Sen adL premium kadın moda markası için stok ve ürün stratejistisin.\n"
            "Hedef: Ürün müdürü, planlama, satın alma ekibi.\n"
            "Dil: Türkçe. Ton: stratejik, veri odaklı, aksiyon yönelimli.\n"
            "Premium marka stok devir hızı hedefi: 3-4 normal, 4-6 sağlıklı.\n"
            "Sezon 6. hafta sell-through benchmark: %30-40 sağlıklı. Altındaysa markdown planı.\n"
            "120+ gün dead stock %5'i geçerse bağlı sermaye kritik — tasfiye stratejisi öner.\n"
            "Beden/renk sapması → bir sonraki sezon sipariş planlaması için kritik sinyal."
            + _FORMAT
        ),
    }
    system = _SYSTEMS.get(rapor_tipi, (
        "Sen adL premium kadın moda markası için kıdemli yönetim danışmanısın.\n"
        "Dil: Türkçe. Ton: yönetici düzeyinde, rakam önce, jargon yok."
        + _FORMAT
    ))

    lines = [f"Rapor tipi: {rapor_tipi}", "", "Veri özeti:"]
    for k, v in ozet_data.items():
        lines.append(f"- {k}: {v}")
    user_msg = "\n".join(lines)

    try:
        raw = await llm_client.complete(system=system, user=user_msg, max_tokens=1500, temperature=0.3)
    except Exception as e:
        return {"acilis_yorumu": f"AI yorumu oluşturulamadı: {e}", "aksiyonlar": []}

    # Parse sections
    import re
    ozet_m  = re.search(r'\[ÖZET\](.*?)(?=\[ANALİZ\]|\[AKSİYONLAR\]|$)', raw, re.DOTALL)
    analiz_m= re.search(r'\[ANALİZ\](.*?)(?=\[AKSİYONLAR\]|$)', raw, re.DOTALL)
    aksiyon_m = re.search(r'\[AKSİYONLAR\](.*?)$', raw, re.DOTALL)

    ozet_text  = ozet_m.group(1).strip()   if ozet_m   else ""
    analiz_text= analiz_m.group(1).strip() if analiz_m else ""
    acilis = (ozet_text + "\n\n" + analiz_text).strip() or raw

    aksiyonlar = []
    if aksiyon_m:
        try:
            import json as _json
            json_str = re.search(r'\[.*\]', aksiyon_m.group(1), re.DOTALL)
            if json_str:
                aksiyonlar = _json.loads(json_str.group())
        except Exception:
            pass

    return {"acilis_yorumu": acilis, "aksiyonlar": aksiyonlar}


@router.get("/product-live/{urun_kodu}")
async def product_live(urun_kodu: str) -> dict:
    """SKU için gerçek zamanlı Pimland verisi: stok, fiyat, finansal, performans.

    Pimland MCP'den paralel çeker — 6 tool eş zamanlı.
    Redis cache: 15 dakika TTL.
    """
    from app.services.product_live import get_product_live
    return await get_product_live(urun_kodu)
