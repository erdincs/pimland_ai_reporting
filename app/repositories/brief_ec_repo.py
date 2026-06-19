"""EC Brief — veritabanı sorguları.

Tek kaynak: incorta_ecommerce_gunluk + incorta_analytics.
Tüm işlemler salt-okunur. Veri yoksa boş liste/dict döner, sayı uydurulmaz.

tarih sütunu TEXT '2026-06-16 00:00:00' formatında;
string karşılaştırma ile YYYY-MM-DD prefix filtresi kullanılır.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Günlük brief için gösterilecek kanallar (domestic + main)
_KANALLAR = (
    "ADL", "ADL IOS APP", "ADL ANDROID APP",
    "TRENDYOL",
    "HEPSIBURADA",
    "BOYNER",
    "LOVEMYBODY",
)

# CSS kanal renk anahtarı  (var(--ch-{key}))
KANAL_RENK: dict[str, str] = {
    "ADL":             "adl",
    "ADL IOS APP":     "adl",
    "ADL ANDROID APP": "adl",
    "TRENDYOL":        "ty",
    "HEPSIBURADA":     "hb",
    "BOYNER":          "hb",
    "LOVEMYBODY":      "az",
}

_KANAL_PARAM = tuple(_KANALLAR)


def _gun_aralik(gun: date) -> tuple[str, str]:
    """'YYYY-MM-DD' başlangıç ve dışlayıcı bitiş."""
    return gun.strftime("%Y-%m-%d"), (gun + timedelta(days=1)).strftime("%Y-%m-%d")


async def net_ciro_kanal(session: AsyncSession, gun: date) -> dict[str, Any]:
    """Bugün + dün kanal bazlı net ciro. Boş sonuç → None döner."""
    bas, son    = _gun_aralik(gun)
    obas, oson  = _gun_aralik(gun - timedelta(days=1))

    sql = text("""
        SELECT satis_kanali,
               SUM(satis_tutar)                                       AS brut,
               SUM(COALESCE(iade_tutar, 0))                          AS iade,
               SUM(COALESCE(iptal_tutar, 0))                         AS iptal,
               SUM(satis_tutar)
                 + SUM(COALESCE(iade_tutar, 0))
                 + SUM(COALESCE(iptal_tutar, 0))                     AS net_ciro,
               SUM(COALESCE(iade_adet, 0))                           AS iade_adet_top,
               SUM(satis_adet)                                       AS satis_adet_top
        FROM   incorta_ecommerce_gunluk
        WHERE  tarih >= :bas AND tarih < :son
          AND  satis_kanali = ANY(:kanallar)
        GROUP  BY satis_kanali
        ORDER  BY net_ciro DESC
    """)
    rows_bugun = (await session.execute(sql, {"bas": bas, "son": son,
                                              "kanallar": list(_KANAL_PARAM)})).mappings().all()

    rows_onceki = (await session.execute(sql, {"bas": obas, "son": oson,
                                               "kanallar": list(_KANAL_PARAM)})).mappings().all()

    if not rows_bugun:
        return {"kanallar": [], "toplam": None, "onceki_toplam": None}

    onceki_map = {r["satis_kanali"]: float(r["net_ciro"] or 0) for r in rows_onceki}

    kanallar = []
    toplam_net = 0.0
    toplam_brut = 0.0
    toplam_iade = 0.0
    for r in rows_bugun:
        net      = float(r["net_ciro"] or 0)
        brut     = float(r["brut"] or 0)
        iade     = float(r["iade"] or 0)
        onceki   = onceki_map.get(r["satis_kanali"], 0)
        delta    = round((net - onceki) / abs(onceki) * 100, 1) if onceki else None
        iade_ort = round(abs(iade) / brut * 100, 1) if brut else 0.0
        toplam_net  += net
        toplam_brut += brut
        toplam_iade += iade
        kanallar.append({
            "satis_kanali": r["satis_kanali"],
            "renk_key":     KANAL_RENK.get(r["satis_kanali"], "adl"),
            "brut":   brut,
            "iade":   iade,
            "net_ciro": net,
            "net_onceki": onceki,
            "delta_pct":  delta,
            "iade_orani": iade_ort,
        })

    onceki_toplam = sum(onceki_map.values())
    toplam_delta  = round((toplam_net - onceki_toplam) / abs(onceki_toplam) * 100, 1) if onceki_toplam else None
    toplam_iade_oran = round(abs(toplam_iade) / toplam_brut * 100, 1) if toplam_brut else 0.0

    return {
        "kanallar":     kanallar,
        "toplam":       toplam_net,
        "toplam_brut":  toplam_brut,
        "toplam_delta": toplam_delta,
        "toplam_iade_orani": toplam_iade_oran,
        "onceki_toplam": onceki_toplam,
    }


async def ga4_trafik(session: AsyncSession, gun: date) -> dict[str, Any]:
    """GA4 trafik özeti. incorta_analytics.date TEXT sütunudur (YYYY-MM-DD)."""
    gun_str = gun.strftime("%Y-%m-%d")
    sql = text("""
        SELECT
            SUM(oturumlar)                                     AS oturumlar,
            SUM(kullanicilar)                                  AS kullanicilar,
            SUM(i_slem_sayisi)                                 AS islem,
            ROUND(AVG(conversion_rate)::numeric * 100, 2)     AS conversion_pct,
            ROUND(AVG(hemen_cikma_orani)::numeric * 100, 1)   AS bounce_pct,
            MAX(date)                                          AS veri_tarihi
        FROM incorta_analytics
        WHERE date = :gun AND marka = 'ADL'
    """)
    row = (await session.execute(sql, {"gun": gun_str})).mappings().one_or_none()

    if not row or not row["oturumlar"]:
        # Stale fallback — son mevcut güne bak
        sql_last = text("""
            SELECT
                SUM(oturumlar) AS oturumlar,
                SUM(kullanicilar) AS kullanicilar,
                SUM(i_slem_sayisi) AS islem,
                ROUND(AVG(conversion_rate)::numeric * 100, 2) AS conversion_pct,
                ROUND(AVG(hemen_cikma_orani)::numeric * 100, 1) AS bounce_pct,
                MAX(date) AS veri_tarihi
            FROM incorta_analytics
            WHERE marka = 'ADL'
              AND date = (SELECT MAX(date) FROM incorta_analytics WHERE marka = 'ADL')
        """)
        row = (await session.execute(sql_last)).mappings().one_or_none()

    if not row or not row["oturumlar"]:
        return {"mevcut": False}

    # Önceki gün delta
    sql_prev = text("""
        SELECT SUM(oturumlar) AS oturumlar,
               ROUND(AVG(conversion_rate)::numeric * 100, 2) AS conversion_pct
        FROM incorta_analytics
        WHERE marka = 'ADL'
          AND date = (SELECT MAX(date) FROM incorta_analytics
                      WHERE marka = 'ADL' AND date < :veri_tarihi)
    """)
    prev = (await session.execute(sql_prev, {"veri_tarihi": row["veri_tarihi"]})).mappings().one_or_none()

    def _delta(cur, prv):
        c, p = float(cur or 0), float(prv or 0)
        return round((c - p) / abs(p) * 100, 1) if p else None

    gun_iso = gun.isoformat()
    veri_tarihi = row["veri_tarihi"]
    return {
        "mevcut":         True,
        "veri_tarihi":    veri_tarihi,
        "stale":          veri_tarihi != gun_iso,
        "oturumlar":      int(row["oturumlar"] or 0),
        "kullanicilar":   int(row["kullanicilar"] or 0),
        "islem":          int(row["islem"] or 0),
        "conversion_pct": float(row["conversion_pct"] or 0),
        "bounce_pct":     float(row["bounce_pct"] or 0),
        "oturum_delta":   _delta(row["oturumlar"], prev["oturumlar"] if prev else None),
        "conv_delta":     _delta(row["conversion_pct"], prev["conversion_pct"] if prev else None),
    }


async def iade_matrisi(session: AsyncSession, gun: date) -> list[dict]:
    """En yüksek iade oranlı ürün × beden × renk (maks 15 satır)."""
    bas, son = _gun_aralik(gun)
    sql = text("""
        SELECT urun_kodu, MAX(urun_adi) AS urun_adi, renk, beden,
               SUM(ABS(COALESCE(iade_adet, 0)))                    AS iade_adet,
               SUM(satis_adet)                                     AS satis_adet,
               ROUND(
                 (SUM(ABS(COALESCE(iade_adet, 0)))
                  / NULLIF(SUM(satis_adet), 0) * 100)::numeric, 1
               )                                                   AS iade_orani
        FROM   incorta_ecommerce_gunluk
        WHERE  tarih >= :bas AND tarih < :son
          AND  satis_kanali = ANY(:kanallar)
        GROUP  BY urun_kodu, renk, beden
        HAVING SUM(ABS(COALESCE(iade_adet, 0))) > 0
        ORDER  BY iade_orani DESC, iade_adet DESC
        LIMIT  15
    """)
    rows = (await session.execute(sql, {"bas": bas, "son": son,
                                        "kanallar": list(_KANAL_PARAM)})).mappings().all()
    return [dict(r) for r in rows]


async def ec_trend_daily(session: AsyncSession, gun: date, period: int = 7) -> list[dict]:
    """Son `period` günün günlük net ciro toplamları (boşluklar 0 ile doldurulur)."""
    start = gun - timedelta(days=period - 1)
    bas = start.strftime("%Y-%m-%d")
    son = (gun + timedelta(days=1)).strftime("%Y-%m-%d")

    sql = text("""
        SELECT
            LEFT(tarih, 10)                                                   AS gun_str,
            SUM(satis_tutar
                + COALESCE(iade_tutar, 0)
                + COALESCE(iptal_tutar, 0))                                   AS net_ciro,
            SUM(satis_tutar)                                                  AS brut_ciro,
            SUM(ABS(COALESCE(iade_adet, 0)))                                  AS iade_adet,
            SUM(satis_adet)                                                   AS satis_adet
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas AND tarih < :son
          AND satis_kanali = ANY(:kanallar)
        GROUP BY LEFT(tarih, 10)
        ORDER BY gun_str
    """)
    rows = (await session.execute(sql, {
        "bas": bas, "son": son, "kanallar": list(_KANAL_PARAM),
    })).mappings().all()

    row_map = {r["gun_str"]: r for r in rows}
    result = []
    for i in range(period):
        g = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        r = row_map.get(g)
        result.append({
            "gun":       g,
            "net_ciro":  float(r["net_ciro"]  or 0) if r else 0.0,
            "brut_ciro": float(r["brut_ciro"] or 0) if r else 0.0,
            "iade_adet": int(r["iade_adet"]   or 0) if r else 0,
            "satis_adet": int(r["satis_adet"] or 0) if r else 0,
        })
    return result


async def ec_period_kanal(session: AsyncSession, gun: date, period: int = 7) -> list[dict]:
    """Son `period` günde kanal bazlı net ciro + pazar payı."""
    start = gun - timedelta(days=period - 1)
    bas = start.strftime("%Y-%m-%d")
    son = (gun + timedelta(days=1)).strftime("%Y-%m-%d")

    sql = text("""
        SELECT satis_kanali,
               SUM(satis_tutar
                   + COALESCE(iade_tutar, 0)
                   + COALESCE(iptal_tutar, 0))  AS net_ciro
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas AND tarih < :son
          AND satis_kanali = ANY(:kanallar)
        GROUP BY satis_kanali
        ORDER BY net_ciro DESC
    """)
    rows = (await session.execute(sql, {
        "bas": bas, "son": son, "kanallar": list(_KANAL_PARAM),
    })).mappings().all()

    total = sum(float(r["net_ciro"] or 0) for r in rows) or 1.0
    return [
        {
            "satis_kanali": r["satis_kanali"],
            "net_ciro":     float(r["net_ciro"] or 0),
            "renk_key":     KANAL_RENK.get(r["satis_kanali"], "adl"),
            "pay_pct":      round(float(r["net_ciro"] or 0) / total * 100, 1),
        }
        for r in rows
    ]


async def top_bottom_sku(session: AsyncSession, gun: date) -> tuple[list[dict], list[dict]]:
    """Net ciroya göre Top 3 ve Bottom 3 SKU."""
    bas, son = _gun_aralik(gun)
    sql = text("""
        SELECT urun_kodu, MAX(urun_adi) AS urun_adi,
               MAX(renk) AS renk,
               SUM(satis_tutar) + SUM(COALESCE(iade_tutar,0))
                 + SUM(COALESCE(iptal_tutar,0))                    AS net_ciro,
               SUM(satis_adet)                                     AS satis_adet
        FROM   incorta_ecommerce_gunluk
        WHERE  tarih >= :bas AND tarih < :son
          AND  satis_kanali = ANY(:kanallar)
        GROUP  BY urun_kodu
        HAVING SUM(satis_tutar) + SUM(COALESCE(iade_tutar,0)) + SUM(COALESCE(iptal_tutar,0)) > 0
    """)
    rows = (await session.execute(sql, {"bas": bas, "son": son,
                                        "kanallar": list(_KANAL_PARAM)})).mappings().all()

    if not rows:
        return [], []

    sıralı = sorted(rows, key=lambda r: float(r["net_ciro"] or 0), reverse=True)
    top3   = [dict(r) for r in sıralı[:3]]
    bot3   = [dict(r) for r in sıralı[-3:]][::-1]   # en kötü önde

    # Önceki gün delta
    obas, oson = _gun_aralik(gun - timedelta(days=1))
    sql_prev = text("""
        SELECT urun_kodu,
               SUM(satis_tutar) + SUM(COALESCE(iade_tutar,0))
                 + SUM(COALESCE(iptal_tutar,0))                    AS net_ciro
        FROM   incorta_ecommerce_gunluk
        WHERE  tarih >= :bas AND tarih < :son
          AND  satis_kanali = ANY(:kanallar)
        GROUP  BY urun_kodu
    """)
    prev_rows = (await session.execute(sql_prev, {"bas": obas, "son": oson,
                                                   "kanallar": list(_KANAL_PARAM)})).mappings().all()
    prev_map = {r["urun_kodu"]: float(r["net_ciro"] or 0) for r in prev_rows}

    def _enrich(lst):
        for item in lst:
            prev = prev_map.get(item["urun_kodu"], 0)
            cur  = float(item["net_ciro"] or 0)
            item["net_onceki"]  = prev
            item["delta_pct"]   = round((cur - prev) / abs(prev) * 100, 1) if prev else None
            item["net_ciro"]    = cur
            item["satis_adet"]  = int(item["satis_adet"] or 0)
        return lst

    return _enrich(top3), _enrich(bot3)
