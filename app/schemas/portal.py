"""Pydantic response schemas for portal API endpoints."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class FilterOptions(BaseModel):
    yillar: List[int]
    aylar: List[int]
    kanallar: List[str]
    renkler: List[str]
    bedenler: List[str]
    markalar: List[str] = []
    sezonlar: List[str] = []
    urun_gruplari: List[str] = []


class KpiSummary(BaseModel):
    brut_ciro: float
    iade_ciro: float
    iptal_ciro: float
    net_ciro: float
    brut_adet: int
    net_adet: int
    iade_oran: float      # %
    iptal_oran: float     # %
    net_obf: float        # net ciro / net adet


class TrendPoint(BaseModel):
    ay: int
    ay_adi: str
    brut_m: float
    iade_m: float
    net_m: float


class KanalRow(BaseModel):
    kanal: str
    ciro: float
    adet: int
    pay: float
    iade_ciro: float
    iade_adet: int
    iade_pay: float
    iade_oran: float


class OverviewData(BaseModel):
    trend: List[TrendPoint]
    kanal: List[KanalRow]


class ProductRow(BaseModel):
    urun_kodu: str
    urun_adi: str
    brut_ciro: float
    net_ciro: float
    iade_ciro: float
    brut_adet: int
    iade_adet: int
    iade_pct: float
    risk_skoru: float
    risk_seviye: str     # KRİTİK | YÜKSEK | ORTA | DÜŞÜK | SAĞLIKLI
    # PLM attributes (may be None before pim_products sync)
    marka_adi: Optional[str] = None
    sezon_adi: Optional[str] = None
    urun_grubu_adi: Optional[str] = None
    ana_grup_adi: Optional[str] = None
    image_url: Optional[str] = None


class ProductList(BaseModel):
    items: List[ProductRow]
    total: int
    page: int
    page_size: int
    total_pages: int


class DrilldownAyRow(BaseModel):
    ay: int
    ay_adi: str
    brut_ciro: float
    iade_ciro: float
    brut_adet: int
    iade_adet: int
    iade_pct: float


class DrilldownDagRow(BaseModel):
    deger: str
    brut_ciro: float
    iade_ciro: float
    adet: int
    iade_pct: float


class ProductDrilldown(BaseModel):
    urun_kodu: str
    urun_adi: str
    brut_ciro: float
    net_ciro: float
    iade_pct: float
    risk_seviye: str
    aylik_trend: List[DrilldownAyRow]
    renk_dagilim: List[DrilldownDagRow]
    beden_dagilim: List[DrilldownDagRow]
    kanal_dagilim: List[DrilldownDagRow]
    # PLM attributes
    marka_adi: Optional[str] = None
    sezon_adi: Optional[str] = None
    urun_grubu_adi: Optional[str] = None
    ana_grup_adi: Optional[str] = None
    first_color_code: Optional[str] = None
    color_codes: Optional[str] = None


class ColorRow(BaseModel):
    renk: str
    brut_ciro: float
    iade_ciro: float
    brut_adet: int
    iade_adet: int
    iade_pct: float
    pay: float
