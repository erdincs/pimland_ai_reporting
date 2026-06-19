"""Eticaret KPI veri servisi — 5 SKL brief şablonu için canlı veri çeker.

Kaynak: incorta_ecommerce_gunluk (günlük kanal/SKU/tutar), incorta_analytics (GA4 trafik),
        enrichment_quality (ürün kalite), sync_jobs (pipeline sağlık)
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger(__name__)


# ── Kanal gruplama ─────────────────────────────────────────────────────────────

_ADL_CHANNELS   = ("ADL", "ADL IOS APP", "ADL ANDROID APP")
_TY_CHANNELS    = ("TRENDYOL", "TY ADL AZ", "TY LMB AZ")
_HB_CHANNELS    = ("HEPSIBURADA",)
_LMB_CHANNELS   = ("LOVEMYBODY", "LMB IOS APP", "LMB ANDROID APP")
_BOYNER_CHANNELS = ("BOYNER",)

_CHANNEL_GROUPS = [
    {"label": "ADL (Web + App)",  "channels": _ADL_CHANNELS,    "color": "orange"},
    {"label": "Trendyol",         "channels": _TY_CHANNELS,     "color": "teal"},
    {"label": "Hepsiburada",      "channels": _HB_CHANNELS,     "color": "blue"},
    {"label": "LovemyBody",       "channels": _LMB_CHANNELS,    "color": "purple"},
    {"label": "Boyner",           "channels": _BOYNER_CHANNELS, "color": "yellow"},
]


def _fmt_try(v: float) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.2f}M ₺"
    if abs(v) >= 1_000:
        return f"{v/1_000:.1f}K ₺"
    return f"{v:,.0f} ₺"


def _pct(a: float, b: float) -> Optional[float]:
    if not b:
        return None
    return round((a - b) / abs(b) * 100, 1)


def _r(v: Any) -> Any:
    return round(float(v), 2) if v is not None else None


# ── SKL-01 · Genel Müdür ──────────────────────────────────────────────────────

async def get_skl01_data(session: AsyncSession) -> dict:
    """KPI strip, kanal nabzı, haftalık/aylık tempo, uyarılar, öncelikler."""

    # --- günlük kanallar (dün + geçen hafta aynı gün) ---
    rows = (await session.execute(text("""
        WITH gunluk AS (
            SELECT
                DATE(tarih) AS gun,
                satis_kanali,
                SUM(COALESCE(satis_tutar, 0))                   AS brut,
                SUM(COALESCE(satis_tutar, 0))
                  + SUM(COALESCE(iade_tutar, 0))
                  + SUM(COALESCE(iptal_tutar, 0))               AS net
            FROM incorta_ecommerce_gunluk
            WHERE DATE(tarih) IN (
                CURRENT_DATE - 1,
                CURRENT_DATE - 8
            )
            GROUP BY DATE(tarih), satis_kanali
        )
        SELECT gun, satis_kanali, brut, net
        FROM gunluk
        ORDER BY gun DESC, brut DESC
    """))).mappings().all()

    from collections import defaultdict
    by_day: Dict[date, Dict[str, Dict]] = defaultdict(dict)
    for r in rows:
        by_day[r["gun"]][r["satis_kanali"]] = {"brut": float(r["brut"] or 0), "net": float(r["net"] or 0)}

    yesterday   = date.today().replace(day=date.today().day - 1)
    last_week   = date.today().replace(day=date.today().day - 8)
    # Use actual dates from the data
    dates_in_data = sorted(by_day.keys(), reverse=True)
    dun   = dates_in_data[0] if dates_in_data else None
    wow_d = dates_in_data[1] if len(dates_in_data) > 1 else None

    def group_total(day_data: Dict, channels: tuple) -> Dict:
        brut = sum(day_data.get(c, {}).get("brut", 0) for c in channels)
        net  = sum(day_data.get(c, {}).get("net",  0) for c in channels)
        return {"brut": brut, "net": net}

    dun_data  = by_day.get(dun, {}) if dun else {}
    wow_data  = by_day.get(wow_d, {}) if wow_d else {}

    # Toplam KPI
    total_brut_dun = sum(v["brut"] for v in dun_data.values())
    total_net_dun  = sum(v["net"]  for v in dun_data.values())
    total_brut_wow = sum(v["brut"] for v in wow_data.values())
    total_net_wow  = sum(v["net"]  for v in wow_data.values())
    makas_pct = round((total_brut_dun - total_net_dun) / total_brut_dun * 100, 1) if total_brut_dun else 0

    # Kanal grupları
    channels_out = []
    for grp in _CHANNEL_GROUPS:
        d = group_total(dun_data, grp["channels"])
        w = group_total(wow_data, grp["channels"])
        if d["brut"] < 100 and d["net"] < 100:
            continue
        share_pct = round(d["net"] / total_net_dun * 100) if total_net_dun else 0
        channels_out.append({
            "name":      grp["label"],
            "color":     grp["color"],
            "ciro":      round(d["net"]),
            "ciro_fmt":  _fmt_try(d["net"]),
            "share_pct": share_pct,
            "wow":       _pct(d["net"], w["net"]),
        })
    channels_out.sort(key=lambda x: -x["ciro"])

    # --- MTD (ay başından bu güne) ---
    mtd = (await session.execute(text("""
        SELECT
            SUM(COALESCE(satis_tutar,0))
              + SUM(COALESCE(iade_tutar,0))
              + SUM(COALESCE(iptal_tutar,0)) AS net,
            COUNT(DISTINCT DATE(tarih)) AS gun_sayisi
        FROM incorta_ecommerce_gunluk
        WHERE EXTRACT(YEAR FROM DATE(tarih))  = EXTRACT(YEAR  FROM CURRENT_DATE)
          AND EXTRACT(MONTH FROM DATE(tarih)) = EXTRACT(MONTH FROM CURRENT_DATE)
          AND DATE(tarih) < CURRENT_DATE
    """))).mappings().first()
    mtd_net      = float(mtd["net"] or 0)
    mtd_gun      = int(mtd["gun_sayisi"] or 1)
    gunluk_ort   = mtd_net / mtd_gun if mtd_gun else 0

    # Geçen ay aynı dönem
    gecen_ay = (await session.execute(text("""
        SELECT SUM(COALESCE(satis_tutar,0))
                 + SUM(COALESCE(iade_tutar,0))
                 + SUM(COALESCE(iptal_tutar,0)) AS net
        FROM incorta_ecommerce_gunluk
        WHERE EXTRACT(YEAR FROM DATE(tarih))  =
                EXTRACT(YEAR FROM CURRENT_DATE - INTERVAL '1 month')
          AND EXTRACT(MONTH FROM DATE(tarih)) =
                EXTRACT(MONTH FROM CURRENT_DATE - INTERVAL '1 month')
          AND DATE_PART('day', DATE(tarih)) < DATE_PART('day', CURRENT_DATE)
    """))).scalar()
    gecen_ay_net = float(gecen_ay or 0)
    mtd_mom_pct  = _pct(mtd_net, gecen_ay_net)

    # Otomatik uyarılar (son 3 gün WoW trendi)
    alerts = _build_skl01_alerts(channels_out, makas_pct)

    return {
        "date":            str(dun) if dun else str(date.today() - __import__("datetime").timedelta(days=1)),
        "kpis": {
            "net_ciro":      round(total_net_dun),
            "net_ciro_fmt":  _fmt_try(total_net_dun),
            "brut_ciro":     round(total_brut_dun),
            "brut_ciro_fmt": _fmt_try(total_brut_dun),
            "makas_pct":     makas_pct,
            "net_wow":       _pct(total_net_dun, total_net_wow),
        },
        "channels": channels_out,
        "tempo": {
            "mtd_net":    round(mtd_net),
            "mtd_fmt":    _fmt_try(mtd_net),
            "gun_sayisi": mtd_gun,
            "gunluk_ort": round(gunluk_ort),
            "ort_fmt":    _fmt_try(gunluk_ort),
            "mom_pct":    mtd_mom_pct,
        },
        "alerts":     alerts,
        "priorities": _build_skl01_priorities(channels_out, makas_pct, total_net_dun),
    }


def _build_skl01_alerts(channels: List, makas_pct: float) -> List:
    alerts = []
    for ch in channels:
        if ch["wow"] is not None and ch["wow"] < -5 and ch["ciro"] > 50_000:
            alerts.append({
                "level": "red",
                "text":  f"{ch['name']} kanalında dün <strong>{ch['wow']:+.1f}%</strong> WoW düşüş — incelenmeli.",
                "code":  "A01",
            })
    if makas_pct > 20:
        alerts.append({
            "level": "red",
            "text":  f"Brüt-Net makas <strong>%{makas_pct:.1f}</strong> — %20 eşiğini aştı. İade+iptal artışı.",
            "code":  "A07",
        })
    elif makas_pct > 15:
        alerts.append({
            "level": "yellow",
            "text":  f"Brüt-Net makas <strong>%{makas_pct:.1f}</strong> — geçen hafta izle.",
            "code":  "A07",
        })
    if not alerts:
        alerts.append({
            "level": "green",
            "text":  "Tüm kanallarda anormal sinyal yok.",
            "code":  "OK",
        })
    return alerts


def _build_skl01_priorities(channels: List, makas_pct: float, net_ciro: float) -> List:
    items = []
    for ch in channels:
        if ch["wow"] is not None and ch["wow"] < -5:
            items.append(f"{ch['name']} kanal düşüşü incelenmeli — WoW {ch['wow']:+.1f}%")
    if makas_pct > 15:
        items.append(f"İade makası %{makas_pct:.1f} — kaynak ürünler tespit edilmeli (SKL-02 detaylı)")
    items.append(f"Günlük ort. {_fmt_try(net_ciro)} ile tempo takibi yapılmalı")
    return [{"num": i + 1, "text": t} for i, t in enumerate(items[:3])]


# ── SKL-02 · Satış & Operasyon ────────────────────────────────────────────────

async def get_skl02_data(session: AsyncSession) -> dict:
    """Net ciro, iade/iptal oranları, top-5 ürün, iade spike, katalog sağlığı."""

    # Ana KPI'lar (dün)
    kpi_row = (await session.execute(text("""
        SELECT
            SUM(COALESCE(satis_tutar,0))
              + SUM(COALESCE(iade_tutar,0))
              + SUM(COALESCE(iptal_tutar,0))          AS net_ciro,
            SUM(COALESCE(satis_tutar,0))               AS brut_ciro,
            SUM(ABS(COALESCE(iade_tutar,0)))           AS iade_tutar,
            SUM(ABS(COALESCE(iptal_tutar,0)))          AS iptal_tutar,
            SUM(COALESCE(satis_adet,0))                AS satis_adet,
            SUM(ABS(COALESCE(iade_adet,0)))            AS iade_adet
        FROM incorta_ecommerce_gunluk
        WHERE DATE(tarih) = CURRENT_DATE - 1
    """))).mappings().first()

    wow_row = (await session.execute(text("""
        SELECT
            SUM(COALESCE(satis_tutar,0))
              + SUM(COALESCE(iade_tutar,0))
              + SUM(COALESCE(iptal_tutar,0))  AS net_ciro,
            SUM(ABS(COALESCE(iade_tutar,0)))  AS iade_tutar,
            SUM(COALESCE(satis_tutar,0))      AS brut_ciro,
            SUM(ABS(COALESCE(iptal_tutar,0))) AS iptal_tutar
        FROM incorta_ecommerce_gunluk
        WHERE DATE(tarih) = CURRENT_DATE - 8
    """))).mappings().first()

    net_dun  = float(kpi_row["net_ciro"]  or 0)
    brut_dun = float(kpi_row["brut_ciro"] or 0)
    iade_dun = float(kpi_row["iade_tutar"] or 0)
    iptal_dun = float(kpi_row["iptal_tutar"] or 0)
    satis_adet = float(kpi_row["satis_adet"] or 1)
    iade_adet  = float(kpi_row["iade_adet"]  or 0)

    iade_oran  = round(iade_dun / brut_dun * 100, 1) if brut_dun else 0
    iptal_oran = round(iptal_dun / brut_dun * 100, 1) if brut_dun else 0

    net_wow  = float(wow_row["net_ciro"]   or 0)
    brut_wow = float(wow_row["brut_ciro"]  or 0)
    iade_wow = float(wow_row["iade_tutar"] or 0)
    iade_oran_wow = round(iade_wow / brut_wow * 100, 1) if brut_wow else 0

    # Top-5 ürün by brüt ciro (dün), max 1 kayıt per ürün kodu
    top5_rows = (await session.execute(text("""
        SELECT
            urun_kodu,
            MAX(urun_adi)                              AS urun_adi,
            MODE() WITHIN GROUP (ORDER BY satis_kanali) AS kanal,
            SUM(COALESCE(satis_tutar,0))               AS ciro,
            SUM(COALESCE(satis_adet,0))                AS adet,
            SUM(COALESCE(satis_tutar,0))
              + SUM(COALESCE(iade_tutar,0))
              + SUM(COALESCE(iptal_tutar,0))           AS net
        FROM incorta_ecommerce_gunluk
        WHERE DATE(tarih) = CURRENT_DATE - 1
          AND COALESCE(satis_tutar,0) > 0
        GROUP BY urun_kodu
        ORDER BY ciro DESC
        LIMIT 5
    """))).mappings().all()

    # WoW for top-5
    top5_codes = [r["urun_kodu"] for r in top5_rows]
    wow_map: Dict[str, float] = {}
    if top5_codes:
        placeholders = ", ".join(f"'{c}'" for c in top5_codes)
        wow_rows = (await session.execute(text(f"""
            SELECT urun_kodu, SUM(COALESCE(satis_tutar,0)) AS ciro
            FROM incorta_ecommerce_gunluk
            WHERE DATE(tarih) = CURRENT_DATE - 8
              AND urun_kodu IN ({placeholders})
            GROUP BY urun_kodu
        """))).mappings().all()
        wow_map = {r["urun_kodu"]: float(r["ciro"]) for r in wow_rows}

    top5_out = []
    for i, r in enumerate(top5_rows):
        ciro = float(r["ciro"] or 0)
        wow_c = wow_map.get(r["urun_kodu"], 0)
        top5_out.append({
            "rank":     i + 1,
            "urun_kodu": r["urun_kodu"],
            "urun_adi":  (r["urun_adi"] or "")[:30],
            "kanal":     r["kanal"] or "",
            "ciro":      round(ciro),
            "ciro_fmt":  _fmt_try(ciro),
            "adet":      int(r["adet"] or 0),
            "wow":       _pct(ciro, wow_c),
        })

    # İade top-5 by iade oranı (dün)
    iade5_rows = (await session.execute(text("""
        SELECT
            urun_kodu,
            MAX(urun_adi) AS urun_adi,
            SUM(ABS(COALESCE(iade_adet,0)))  AS iade_a,
            SUM(COALESCE(satis_adet,0))      AS satis_a
        FROM incorta_ecommerce_gunluk
        WHERE DATE(tarih) = CURRENT_DATE - 1
          AND COALESCE(satis_adet,0) > 0
        GROUP BY urun_kodu
        HAVING SUM(COALESCE(satis_adet,0)) >= 5
        ORDER BY SUM(ABS(COALESCE(iade_adet,0))) / NULLIF(SUM(COALESCE(satis_adet,0)),0) DESC
        LIMIT 5
    """))).mappings().all()

    iade5_out = []
    for r in iade5_rows:
        ia = float(r["iade_a"] or 0)
        sa = float(r["satis_a"] or 1)
        oran = round(ia / sa * 100, 1)
        iade5_out.append({
            "urun_kodu": r["urun_kodu"],
            "urun_adi":  (r["urun_adi"] or "")[:30],
            "iade_oran": oran,
            "iade_adet": int(ia),
            "satis_adet": int(sa),
        })

    # Sıfır satış (7 gün aktif ürün ama satış yok)
    sifir_satis = (await session.execute(text("""
        SELECT COUNT(DISTINCT p.urun_kodu) AS cnt
        FROM pim_products p
        WHERE p.internet_aktif = true
          AND NOT EXISTS (
              SELECT 1 FROM incorta_ecommerce_gunluk e
              WHERE e.urun_kodu = p.urun_kodu
                AND DATE(e.tarih) >= CURRENT_DATE - 7
                AND COALESCE(e.satis_adet, 0) > 0
          )
    """))).scalar() or 0

    # Katalog sağlığı
    katalog = (await session.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE internet_aktif = true)  AS aktif,
            COUNT(*) FILTER (WHERE internet_aktif = false) AS pasif,
            COUNT(*) FILTER (WHERE bloke = true)           AS bloke,
            COUNT(*)                                       AS toplam
        FROM pim_products
    """))).mappings().first()

    # Sezon dağılımı
    sezon_row = (await session.execute(text("""
        SELECT sezon_adi,
            COUNT(*) AS toplam,
            COUNT(*) FILTER (WHERE internet_aktif = true)  AS yayinda
        FROM pim_products
        WHERE sezon_adi IS NOT NULL AND sezon_adi != ''
        GROUP BY sezon_adi
        ORDER BY toplam DESC
        LIMIT 1
    """))).mappings().first()

    return {
        "kpis": {
            "net_ciro":     round(net_dun),
            "net_ciro_fmt": _fmt_try(net_dun),
            "net_wow":      _pct(net_dun, net_wow),
            "iade_oran":    iade_oran,
            "iade_wow":     round(iade_oran - iade_oran_wow, 1),
            "iptal_oran":   iptal_oran,
            "sifir_satis":  int(sifir_satis),
        },
        "top5":   top5_out,
        "iade5":  iade5_out,
        "katalog": {
            "aktif":      int(katalog["aktif"]  or 0),
            "pasif":      int(katalog["pasif"]  or 0),
            "bloke":      int(katalog["bloke"]  or 0),
            "toplam":     int(katalog["toplam"] or 0),
            "sezon_adi":  sezon_row["sezon_adi"]  if sezon_row else "—",
            "sezon_toplam": int(sezon_row["toplam"]) if sezon_row else 0,
            "sezon_yayinda": int(sezon_row["yayinda"]) if sezon_row else 0,
        },
    }


# ── SKL-03 · Ürün & Katalog ───────────────────────────────────────────────────

async def get_skl03_data(session: AsyncSession) -> dict:
    """Enrichment grade dağılımı, site senkron, sıralama sağlığı, sezon durumu."""

    # Enrichment grade dağılımı
    grade_rows = (await session.execute(text("""
        SELECT quality_grade,
               COUNT(*) AS cnt
        FROM enrichment_quality
        GROUP BY quality_grade
        ORDER BY quality_grade
    """))).mappings().all()

    total_enrichment = sum(int(r["cnt"]) for r in grade_rows)
    grades = {}
    for r in grade_rows:
        g = r["quality_grade"] or "?"
        cnt = int(r["cnt"])
        grades[g] = {
            "cnt": cnt,
            "pct": round(cnt / total_enrichment * 100, 1) if total_enrichment else 0,
        }
    grade_list = [
        {"grade": g, **grades.get(g, {"cnt": 0, "pct": 0})}
        for g in ("A", "B", "C", "D", "F")
    ]

    # PIM ürün stats
    pim = (await session.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE internet_aktif = true)         AS aktif,
            COUNT(*) FILTER (WHERE internet_aktif = false)        AS pasif,
            COUNT(*) FILTER (WHERE bloke = true)                  AS bloke,
            COUNT(*) FILTER (WHERE default_image_url IS NULL
                             AND internet_aktif = true)           AS gorselsiz_aktif,
            COUNT(*)                                              AS toplam
        FROM pim_products
    """))).mappings().first()

    # Sıralama durumu
    siralama = (await session.execute(text("""
        SELECT
            COUNT(*) AS toplam_is,
            COUNT(*) FILTER (WHERE onay_tarihi < NOW() - INTERVAL '14 days') AS eskimis_14,
            COUNT(*) FILTER (WHERE onay_tarihi < NOW() - INTERVAL '30 days') AS eskimis_30
        FROM siralama_gecmisi
        WHERE onayli = true
    """))).mappings().first()

    # Son sıralama işleri
    son_siralama = (await session.execute(text("""
        SELECT kategori, onay_tarihi,
               ROUND(EXTRACT(EPOCH FROM (NOW() - onay_tarihi)) / 86400) AS gun_once
        FROM siralama_gecmisi
        WHERE onayli = true AND onay_tarihi < NOW() - INTERVAL '14 days'
        ORDER BY onay_tarihi ASC
        LIMIT 5
    """))).mappings().all()

    # Sezon durumu
    sezon_rows = (await session.execute(text("""
        SELECT sezon_adi,
               COUNT(*) AS toplam,
               COUNT(*) FILTER (WHERE internet_aktif = true)  AS yayinda,
               COUNT(*) FILTER (WHERE internet_aktif = false) AS bekleyen
        FROM pim_products
        WHERE sezon_adi IS NOT NULL AND sezon_adi != ''
        GROUP BY sezon_adi
        ORDER BY toplam DESC
        LIMIT 3
    """))).mappings().all()

    # Dün enrichment aktivitesi
    dun_enrichment = (await session.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE DATE(last_scored_at) = CURRENT_DATE - 1) AS dun_islem,
            COUNT(*) FILTER (WHERE quality_grade = 'A'
                             AND DATE(last_scored_at) = CURRENT_DATE - 1) AS dun_grade_a
        FROM enrichment_quality
    """))).mappings().first()

    return {
        "grades":    grade_list,
        "total_enrichment": total_enrichment,
        "pim": {
            "aktif":          int(pim["aktif"] or 0),
            "pasif":          int(pim["pasif"] or 0),
            "bloke":          int(pim["bloke"] or 0),
            "toplam":         int(pim["toplam"] or 0),
            "gorselsiz_aktif": int(pim["gorselsiz_aktif"] or 0),
        },
        "siralama": {
            "toplam_is":   int(siralama["toplam_is"] or 0),
            "eskimis_14":  int(siralama["eskimis_14"] or 0),
            "eskimis_30":  int(siralama["eskimis_30"] or 0),
            "stale_cats": [
                {
                    "kategori": r["kategori"],
                    "gun_once": int(r["gun_once"] or 0),
                }
                for r in son_siralama
            ],
        },
        "sezonlar": [
            {
                "sezon_adi": r["sezon_adi"],
                "toplam":    int(r["toplam"]),
                "yayinda":   int(r["yayinda"]),
                "bekleyen":  int(r["bekleyen"]),
                "yayin_pct": round(int(r["yayinda"]) / int(r["toplam"]) * 100, 1) if r["toplam"] else 0,
            }
            for r in sezon_rows
        ],
        "dun_enrichment": {
            "islem":   int(dun_enrichment["dun_islem"] or 0),
            "grade_a": int(dun_enrichment["dun_grade_a"] or 0),
        },
    }


# ── SKL-04 · Pazarlama ────────────────────────────────────────────────────────

async def get_skl04_data(session: AsyncSession) -> dict:
    """Kanal performansı (e-ticaret), trafik kalitesi (analytics), karar listesi."""

    # Kanal performansı (dün + WoW)
    kanal_rows = (await session.execute(text("""
        WITH base AS (
            SELECT
                DATE(tarih) AS gun,
                CASE
                    WHEN satis_kanali IN ('ADL','ADL IOS APP','ADL ANDROID APP') THEN 'adl.com.tr'
                    WHEN satis_kanali IN ('TRENDYOL','TY ADL AZ','TY LMB AZ')   THEN 'Trendyol'
                    WHEN satis_kanali = 'HEPSIBURADA'                            THEN 'Hepsiburada'
                    WHEN satis_kanali IN ('LOVEMYBODY','LMB IOS APP','LMB ANDROID APP') THEN 'LovemyBody'
                    WHEN satis_kanali = 'BOYNER'                                 THEN 'Boyner'
                    ELSE satis_kanali
                END AS kanal,
                SUM(COALESCE(satis_tutar,0))
                  + SUM(COALESCE(iade_tutar,0))
                  + SUM(COALESCE(iptal_tutar,0)) AS net
            FROM incorta_ecommerce_gunluk
            WHERE DATE(tarih) IN (CURRENT_DATE - 1, CURRENT_DATE - 8)
            GROUP BY DATE(tarih), kanal
        )
        SELECT
            kanal,
            SUM(net) FILTER (WHERE gun = CURRENT_DATE - 1) AS net_dun,
            SUM(net) FILTER (WHERE gun = CURRENT_DATE - 8) AS net_wow
        FROM base
        GROUP BY kanal
        ORDER BY net_dun DESC NULLS LAST
    """))).mappings().all()

    total_net_dun = sum(float(r["net_dun"] or 0) for r in kanal_rows)

    kanallar_out = []
    for r in kanal_rows:
        net_d = float(r["net_dun"] or 0)
        net_w = float(r["net_wow"] or 0)
        if net_d < 1000:
            continue
        kanallar_out.append({
            "kanal":    r["kanal"],
            "net":      round(net_d),
            "net_fmt":  _fmt_try(net_d),
            "share":    round(net_d / total_net_dun * 100, 1) if total_net_dun else 0,
            "wow":      _pct(net_d, net_w),
        })

    # Analytics trafik kalitesi
    ana_rows = (await session.execute(text("""
        SELECT
            oturum_kaynagi,
            SUM(oturumlar)           AS sess,
            SUM(ciro)                AS ciro,
            AVG(conversion_rate)     AS cvr,
            AVG(hemen_cikma_orani)   AS bounce
        FROM incorta_analytics
        WHERE date::date = CURRENT_DATE - 1
        GROUP BY oturum_kaynagi
        ORDER BY ciro DESC NULLS LAST
    """))).mappings().all()

    # Analytics boşsa özet göster
    has_analytics = len(ana_rows) > 0
    analytics_out = []
    for r in ana_rows:
        analytics_out.append({
            "kaynak": r["oturum_kaynagi"] or "diğer",
            "sess":   int(r["sess"] or 0),
            "ciro":   round(float(r["ciro"] or 0)),
            "cvr":    round(float(r["cvr"] or 0) * 100, 1),
            "bounce": round(float(r["bounce"] or 0) * 100, 1),
        })

    # Otomatik karar listesi
    kararlar = _build_skl04_kararlar(kanallar_out)

    return {
        "kanallar":      kanallar_out,
        "total_net_fmt": _fmt_try(total_net_dun),
        "has_analytics": has_analytics,
        "analytics":     analytics_out,
        "kararlar":      kararlar,
    }


def _build_skl04_kararlar(kanallar: List) -> List:
    items = []
    for ch in kanallar:
        if ch["wow"] is not None and ch["wow"] < -5:
            items.append({
                "text": f"{ch['kanal']} kanalı WoW {ch['wow']:+.1f}% — kanal yöneticisiyle incelenmeli",
                "who": "Kanal Yöneticisi",
            })
    if not items:
        items.append({"text": "Tüm kanallarda anormal sinyal yok — rutin takip", "who": "Ekip"})
    items.append({
        "text": "Analytics entegrasyonu (GA4 / Incorta) aktifleştirilince bu bölüm trafik+dönüşüm verisiyle dolacak",
        "who": "Teknik Ekip",
    })
    return items[:4]


# ── SKL-05 · Teknoloji & Süreç ────────────────────────────────────────────────

async def get_skl05_data(session: AsyncSession) -> dict:
    """Sistem sağlığı, pipeline durumu, ekip üretkenliği, teknik sorunlar."""

    # Son sync jobları
    sync_rows = (await session.execute(text("""
        SELECT source_id, status, rows_loaded, duration_ms,
               started_at, finished_at
        FROM sync_jobs
        WHERE finished_at > NOW() - INTERVAL '24 hours'
           OR (status = 'running' AND started_at > NOW() - INTERVAL '2 hours')
        ORDER BY COALESCE(finished_at, started_at) DESC
        LIMIT 20
    """))).mappings().all()

    seen: set = set()
    pipeline: List[Dict] = []
    for r in sync_rows:
        src = r["source_id"]
        if src in seen:
            continue
        seen.add(src)
        dur_sn = int((r["duration_ms"] or 0) / 1000)
        pipeline.append({
            "source_id":  src,
            "status":     r["status"],
            "rows":       int(r["rows_loaded"] or 0),
            "dur_sn":     dur_sn,
            "dur_fmt":    f"{dur_sn // 60}dk {dur_sn % 60}sn" if dur_sn >= 60 else f"{dur_sn}sn",
            "finished_at": str(r["finished_at"])[:16] if r["finished_at"] else "Devam ediyor",
        })

    # PIM stats
    pim = (await session.execute(text("""
        SELECT
            COUNT(*) AS toplam,
            COUNT(*) FILTER (WHERE internet_aktif) AS aktif
        FROM pim_products
    """))).mappings().first()

    # Enrichment üretkenlik (dün)
    enr = (await session.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE DATE(last_scored_at) = CURRENT_DATE - 1) AS dun,
            COUNT(*) FILTER (WHERE quality_grade = 'A')                     AS grade_a_toplam,
            COUNT(*) FILTER (WHERE quality_grade IN ('D','F'))              AS df_bekleyen
        FROM enrichment_quality
    """))).mappings().first()

    # Sıralama üretkenlik (dün)
    sir = (await session.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE DATE(onay_tarihi) = CURRENT_DATE - 1) AS dun,
            COUNT(*) FILTER (WHERE onay_tarihi < NOW() - INTERVAL '14 days') AS eskimis
        FROM siralama_gecmisi
        WHERE onayli = true
    """))).mappings().first()

    # Teknik sorunlar: PIM fiyat farkı, görselsiz ürün
    sorunlar = []

    gorselsiz = (await session.execute(text("""
        SELECT COUNT(*) FROM pim_products
        WHERE internet_aktif = true AND default_image_url IS NULL
    """))).scalar() or 0

    if gorselsiz > 0:
        sorunlar.append({
            "severity": "yellow",
            "title":    f"{gorselsiz} görselsiz aktif ürün",
            "impact":   "Müşteri görsel göremez, dönüşüm düşer. Görsel yükle ve yayınla.",
            "tag":      "Orta",
        })

    if sir["eskimis"] and int(sir["eskimis"]) > 0:
        sorunlar.append({
            "severity": "blue",
            "title":    f"{sir['eskimis']} kategori 14+ gün sıralama güncellenmedi",
            "impact":   "Sıralama skorları eskimiş — satış potansiyeli kaçırılıyor.",
            "tag":      "Düşük",
        })

    if not sorunlar:
        sorunlar.append({
            "severity": "green",
            "title":    "Kritik teknik sorun tespit edilmedi",
            "impact":   "Rutin izleme devam ediyor.",
            "tag":      "OK",
        })

    # Genel sistem durumu
    fail_count = sum(1 for p in pipeline if p["status"] == "failed")
    sys_status = "red" if fail_count >= 3 else ("yellow" if fail_count >= 1 else "green")
    sys_text   = (
        "Kritik hata var — incelenmeli" if sys_status == "red"
        else ("Bazı sorunlar mevcut" if sys_status == "yellow"
              else "Tüm Sistemler Normal")
    )

    return {
        "sys_status": sys_status,
        "sys_text":   sys_text,
        "pipeline":   pipeline,
        "pim": {
            "toplam": int(pim["toplam"] or 0),
            "aktif":  int(pim["aktif"]  or 0),
        },
        "enrichment": {
            "dun":         int(enr["dun"] or 0),
            "hedef":       50,
            "grade_a":     int(enr["grade_a_toplam"] or 0),
            "df_bekleyen": int(enr["df_bekleyen"] or 0),
        },
        "siralama": {
            "dun":     int(sir["dun"] or 0),
            "hedef":   15,
            "eskimis": int(sir["eskimis"] or 0),
        },
        "sorunlar": sorunlar,
    }
