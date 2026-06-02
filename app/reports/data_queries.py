"""SQL data collection for the e-commerce monthly report.

All queries run on the read-only session. Returns plain dicts/lists
so the HTML renderer has no SQLAlchemy dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class ExecKpis:
    brut_ciro: float = 0.0
    iade_ciro: float = 0.0
    iptal_ciro: float = 0.0
    net_ciro: float = 0.0
    brut_adet: int = 0
    net_adet: int = 0
    iade_oran: float = 0.0    # % brüt ciro bazında
    net_obf: float = 0.0      # net ciro / net adet

    @property
    def net_ciro_calc(self) -> float:
        return self.brut_ciro - self.iade_ciro - self.iptal_ciro


@dataclass
class KanalRow:
    kanal: str
    ciro: float
    adet: int
    pay: float


@dataclass
class ProductRow:
    rank: int
    urun_kodu: str
    urun_adi: str
    brut_ciro: float
    net_ciro: float
    brut_adet: int
    iade_pct: float


@dataclass
class TrendRow:
    ay: int
    brut_m: float
    iade_m: float
    net_m: float


@dataclass
class ReportData:
    yil: int
    ay: int
    ay_adi: str
    kpis: ExecKpis = field(default_factory=ExecKpis)
    kanal_satis: List[KanalRow] = field(default_factory=list)
    kanal_iade: List[KanalRow] = field(default_factory=list)
    top_urunler: List[ProductRow] = field(default_factory=list)
    trend: List[TrendRow] = field(default_factory=list)


_AY_ADI = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}


async def collect(session: AsyncSession, yil: int, ay: int) -> ReportData:
    data = ReportData(yil=yil, ay=ay, ay_adi=_AY_ADI.get(ay, str(ay)))

    # ── 1. Executive KPIs ─────────────────────────────────────────────────
    row = (await session.execute(text("""
        SELECT
            COALESCE(SUM(s.tutar), 0)                                         AS brut_ciro,
            ABS(COALESCE(SUM(d.iade),   0))                                   AS iade_ciro,
            ABS(COALESCE(SUM(ip.iptal), 0))                                   AS iptal_ciro,
            COALESCE(SUM(s.adet),  0)                                         AS brut_adet,
            COALESCE(SUM(s.adet),0) + COALESCE(SUM(d.iade_adet),0)
              + COALESCE(SUM(ip.iptal_adet),0)                                AS net_adet
        FROM incorta_satis s
        LEFT JOIN (
            SELECT urun_kodu,satis_kanali,renk,beden,
                   SUM(tutar) iade, SUM(adet) iade_adet
            FROM incorta_depo_iade WHERE ay=:ay AND yil=:yil GROUP BY 1,2,3,4
        ) d ON s.urun_kodu=d.urun_kodu AND s.satis_kanali=d.satis_kanali
              AND s.renk=d.renk AND s.beden=d.beden
        LEFT JOIN (
            SELECT urun_kodu,satis_kanali,renk,beden,
                   SUM(tutar) iptal, SUM(adet) iptal_adet
            FROM incorta_iptal_siparis WHERE ay=:ay AND yil=:yil GROUP BY 1,2,3,4
        ) ip ON s.urun_kodu=ip.urun_kodu AND s.satis_kanali=ip.satis_kanali
               AND s.renk=ip.renk AND s.beden=ip.beden
        WHERE s.ay=:ay AND s.yil=:yil
    """), {"ay": ay, "yil": yil})).mappings().first()

    if row:
        brut = float(row["brut_ciro"] or 0)
        iade = float(row["iade_ciro"] or 0)
        iptal = float(row["iptal_ciro"] or 0)
        net_adet = int(row["net_adet"] or 0)
        data.kpis = ExecKpis(
            brut_ciro=brut,
            iade_ciro=iade,
            iptal_ciro=iptal,
            net_ciro=brut - iade - iptal,
            brut_adet=int(row["brut_adet"] or 0),
            net_adet=net_adet,
            iade_oran=round(iade / brut * 100, 1) if brut else 0.0,
            net_obf=round((brut - iade - iptal) / net_adet, 0) if net_adet else 0.0,
        )

    # ── 2. Kanal satış ────────────────────────────────────────────────────
    rows = (await session.execute(text("""
        SELECT satis_kanali,
               SUM(tutar)                                              AS ciro,
               SUM(adet::int)                                         AS adet,
               (100.0*SUM(tutar)/SUM(SUM(tutar))OVER())::numeric(5,1) AS pay
        FROM incorta_satis WHERE ay=:ay AND yil=:yil
        GROUP BY satis_kanali ORDER BY SUM(tutar) DESC LIMIT 8
    """), {"ay": ay, "yil": yil})).mappings().all()
    data.kanal_satis = [
        KanalRow(r["satis_kanali"], float(r["ciro"]), int(r["adet"]), float(r["pay"]))
        for r in rows
    ]

    # ── 3. Kanal iade ─────────────────────────────────────────────────────
    rows = (await session.execute(text("""
        SELECT satis_kanali,
               ABS(SUM(tutar))                                                    AS iade,
               ABS(SUM(adet::int))                                                AS iade_adet,
               (100.0*ABS(SUM(tutar))/SUM(ABS(SUM(tutar)))OVER())::numeric(5,1)   AS pay
        FROM incorta_depo_iade WHERE ay=:ay AND yil=:yil
        GROUP BY satis_kanali ORDER BY ABS(SUM(tutar)) DESC LIMIT 6
    """), {"ay": ay, "yil": yil})).mappings().all()
    data.kanal_iade = [
        KanalRow(r["satis_kanali"], float(r["iade"]), int(r["iade_adet"]), float(r["pay"]))
        for r in rows
    ]

    # ── 4. Top 10 ürün ────────────────────────────────────────────────────
    rows = (await session.execute(text("""
        WITH s AS (
            SELECT urun_kodu, urun_adi,
                   SUM(tutar) brut, SUM(adet) brut_adet
            FROM incorta_satis WHERE ay=:ay AND yil=:yil GROUP BY 1,2
        ),
        i AS (
            SELECT urun_kodu, SUM(tutar) iade
            FROM incorta_depo_iade WHERE ay=:ay AND yil=:yil GROUP BY 1
        )
        SELECT s.urun_kodu, s.urun_adi,
               s.brut::double precision               AS brut_ciro,
               (s.brut + COALESCE(i.iade,0))::double precision AS net_ciro,
               s.brut_adet::int                       AS brut_adet,
               (ABS(COALESCE(i.iade,0))/NULLIF(s.brut,0)*100)::numeric(5,1) AS iade_pct
        FROM s LEFT JOIN i ON s.urun_kodu=i.urun_kodu
        ORDER BY net_ciro DESC LIMIT 10
    """), {"ay": ay, "yil": yil})).mappings().all()
    data.top_urunler = [
        ProductRow(
            rank=idx + 1,
            urun_kodu=r["urun_kodu"],
            urun_adi=r["urun_adi"],
            brut_ciro=float(r["brut_ciro"]),
            net_ciro=float(r["net_ciro"]),
            brut_adet=int(r["brut_adet"]),
            iade_pct=float(r["iade_pct"] or 0),
        )
        for idx, r in enumerate(rows)
    ]

    # ── 5. Aylık trend ────────────────────────────────────────────────────
    rows = (await session.execute(text("""
        SELECT s.ay,
               (SUM(s.tutar)/1000000)::numeric(8,1)         AS brut_m,
               (ABS(SUM(COALESCE(d.tutar,0)))/1000000)::numeric(8,1) AS iade_m
        FROM incorta_satis s
        LEFT JOIN incorta_depo_iade d
               ON s.urun_kodu=d.urun_kodu AND s.ay=d.ay AND s.yil=d.yil
              AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        WHERE s.yil=:yil
        GROUP BY s.ay ORDER BY s.ay
    """), {"yil": yil})).mappings().all()
    data.trend = [
        TrendRow(
            ay=r["ay"],
            brut_m=float(r["brut_m"]),
            iade_m=float(r["iade_m"]),
            net_m=round(float(r["brut_m"]) - float(r["iade_m"]), 1),
        )
        for r in rows
    ]

    return data
