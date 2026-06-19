"""MG Brief — veritabanı sorguları.

Kaynaklar:
  incorta_magaza_gunluk   — SKU-level günlük satış/iade (tarih TEXT)
  incorta_magaza_performans — aylık KPIlar (hedef, mdo, obf, sepet — TEXT)

Önemli notlar:
  - tarih TEXT '2026-06-16 00:00:00' → string prefix filtresi
  - performans'ta mdo/hedef_orani 0-1 ondalık → ×100 ile yüzdeye çevrilir
  - Performans '--' değerleri NULLIF ile NULL'a dönüştürülür
  - bolge_muduru IS NULL AND magaza IS NULL → toplam satırı
  - bolge_muduru IS NOT NULL AND magaza IS NULL → bölge satırı
  - bolge_muduru IS NOT NULL AND magaza IS NOT NULL → mağaza satırı
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _gun_aralik(gun: date) -> tuple[str, str]:
    return gun.strftime("%Y-%m-%d"), (gun + timedelta(days=1)).strftime("%Y-%m-%d")


def _safe_float(val: Any) -> float | None:
    """TEXT sütunları için güvenli cast; None / '--' → None."""
    if val is None or val == '--':
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


async def ozet_kpi(session: AsyncSession, gun: date) -> dict[str, Any]:
    """Hero + aylık MDO/OBF/Sepet (günlük net ciro + ayın KPIları).

    Returns:
        {
          gun_net: float|None, gun_onceki_net: float|None, gun_delta_pct: float|None,
          ay_hedef: float|None, ay_net: float|None, ay_doluluk: float|None,
          mdo: float|None, obf: float|None, sepet: float|None, ziyaretci: int|None,
          ay_mevcut: bool
        }
    """
    bas, son = _gun_aralik(gun)
    obas, oson = _gun_aralik(gun - timedelta(days=1))

    # Günlük net ciro (gunluk tablosu)
    sql_gun = text("""
        SELECT
            SUM(COALESCE(satis_tutar, 0))   AS satis,
            SUM(COALESCE(iade_tutari, 0))   AS iade
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas AND tarih < :son
    """)
    row_gun = (await session.execute(sql_gun, {"bas": bas, "son": son})).mappings().one_or_none()
    row_onceki = (await session.execute(sql_gun, {"bas": obas, "son": oson})).mappings().one_or_none()

    gun_net = float((row_gun["satis"] or 0) + (row_gun["iade"] or 0)) if row_gun else None
    onceki_net = float((row_onceki["satis"] or 0) + (row_onceki["iade"] or 0)) if row_onceki else None
    gun_delta = (
        round((gun_net - onceki_net) / abs(onceki_net) * 100, 1)
        if gun_net and onceki_net else None
    )

    # Aylık KPIlar (performans tablosu) — ay geneli (bolge_muduru/magaza NULL)
    sql_ay = text("""
        SELECT
            NULLIF(hedef, '--')::numeric         AS hedef,
            NULLIF(net_ciro, '--')::numeric      AS net_ciro,
            NULLIF(hedef_orani, '--')::numeric   AS hedef_orani,
            NULLIF(ziyaretci, '--')::numeric     AS ziyaretci,
            NULLIF(mdo, '--')::numeric           AS mdo,
            NULLIF(sepet, '--')::numeric         AS sepet,
            NULLIF(obf, '--')::numeric           AS obf
        FROM incorta_magaza_performans
        WHERE yil = :yil AND ROUND(ay::numeric) = :ay
          AND bolge_muduru IS NULL AND magaza IS NULL
        LIMIT 1
    """)
    row_ay = (await session.execute(sql_ay, {"yil": gun.year, "ay": gun.month})).mappings().one_or_none()

    ay_mevcut = row_ay is not None and row_ay["net_ciro"] is not None
    hedef      = _safe_float(row_ay["hedef"])      if ay_mevcut else None
    ay_net     = _safe_float(row_ay["net_ciro"])   if ay_mevcut else None
    hedef_oran = _safe_float(row_ay["hedef_orani"]) if ay_mevcut else None
    mdo_raw    = _safe_float(row_ay["mdo"])        if ay_mevcut else None
    sepet_raw  = _safe_float(row_ay["sepet"])      if ay_mevcut else None
    obf_raw    = _safe_float(row_ay["obf"])        if ay_mevcut else None
    ziy_raw    = _safe_float(row_ay["ziyaretci"])  if ay_mevcut else None

    return {
        "gun_net":        gun_net,
        "gun_onceki_net": onceki_net,
        "gun_delta_pct":  gun_delta,
        "ay_hedef":       hedef,
        "ay_net":         ay_net,
        # hedef_orani 0-1 skala → yüzde
        "ay_doluluk":     round(hedef_oran * 100, 1) if hedef_oran is not None else None,
        # mdo 0-1 skala → yüzde
        "mdo":            round(mdo_raw * 100, 2) if mdo_raw is not None else None,
        "sepet":          round(sepet_raw, 2) if sepet_raw is not None else None,
        "obf":            round(obf_raw, 0) if obf_raw is not None else None,
        "ziyaretci":      int(ziy_raw) if ziy_raw is not None else None,
        "ay_mevcut":      ay_mevcut,
    }


async def bolge_muduru_tablosu(session: AsyncSession, gun: date) -> list[dict[str, Any]]:
    """Bölge müdürü bazında aylık performans (15 satır, doluluk sıralı).

    Returns: [{bolge_muduru, magaza_sayisi, net_ciro, hedef, doluluk, mdo}]
    """
    # Per-mağaza satırları toplayarak bölge bazlı özet üret.
    # hedef_orani / mdo: bölge aggregate satırından al (magaza IS NULL).
    sql = text("""
        WITH magaza_toplam AS (
            SELECT bolge_muduru,
                   COUNT(DISTINCT magaza)                           AS magaza_sayisi,
                   SUM(NULLIF(net_ciro, '--')::numeric)             AS net_ciro,
                   SUM(NULLIF(hedef, '--')::numeric)                AS hedef
            FROM   incorta_magaza_performans
            WHERE  yil = :yil AND ROUND(ay::numeric) = :ay
              AND  bolge_muduru IS NOT NULL AND magaza IS NOT NULL
            GROUP  BY bolge_muduru
        ),
        bolge_kpi AS (
            SELECT bolge_muduru,
                   NULLIF(hedef_orani, '--')::numeric AS hedef_orani,
                   NULLIF(mdo, '--')::numeric         AS mdo
            FROM   incorta_magaza_performans
            WHERE  yil = :yil AND ROUND(ay::numeric) = :ay
              AND  bolge_muduru IS NOT NULL AND magaza IS NULL
        )
        SELECT  m.bolge_muduru, m.magaza_sayisi, m.net_ciro, m.hedef,
                ROUND(m.net_ciro / NULLIF(m.hedef, 0) * 100, 1) AS hedef_orani,
                k.mdo
        FROM    magaza_toplam m
        LEFT    JOIN bolge_kpi k USING (bolge_muduru)
        ORDER   BY hedef_orani DESC NULLS LAST
        LIMIT   15
    """)
    rows = (await session.execute(sql, {"yil": gun.year, "ay": gun.month})).mappings().all()

    result = []
    for r in rows:
        doluluk_raw = _safe_float(r["hedef_orani"])
        mdo_raw     = _safe_float(r["mdo"])
        result.append({
            "bolge_muduru":  r["bolge_muduru"],
            "magaza_sayisi": int(r["magaza_sayisi"] or 0),
            "net_ciro":      _safe_float(r["net_ciro"]),
            "hedef":         _safe_float(r["hedef"]),
            # hedef_orani burada zaten % (0-100 arası) — CTE'de ROUND × 100 yapıldı
            "doluluk":       doluluk_raw,
            # mdo hâlâ 0-1 skala (bolge_kpi'dan geliyor)
            "mdo":           round(mdo_raw * 100, 2) if mdo_raw is not None else None,
        })
    return result


async def top_bottom_magaza(session: AsyncSession, gun: date) -> tuple[list[dict], list[dict]]:
    """Hedef gerçekleştirmeye göre Top 5 / Bottom 5 mağaza.

    Returns: (top5, bot5)
    """
    sql = text("""
        SELECT
            magaza, bolge_muduru,
            NULLIF(net_ciro, '--')::numeric     AS net_ciro,
            NULLIF(hedef, '--')::numeric        AS hedef,
            NULLIF(hedef_orani, '--')::numeric  AS hedef_orani,
            NULLIF(mdo, '--')::numeric          AS mdo,
            NULLIF(obf, '--')::numeric          AS obf,
            NULLIF(sepet, '--')::numeric        AS sepet
        FROM incorta_magaza_performans
        WHERE yil = :yil AND ROUND(ay::numeric) = :ay
          AND bolge_muduru IS NOT NULL AND magaza IS NOT NULL
          AND NULLIF(hedef_orani, '--') IS NOT NULL
    """)
    rows = (await session.execute(sql, {"yil": gun.year, "ay": gun.month})).mappings().all()

    if not rows:
        return [], []

    def _enrich(r) -> dict:
        dol_raw = _safe_float(r["hedef_orani"])
        mdo_raw = _safe_float(r["mdo"])
        return {
            "magaza":       r["magaza"],
            "bolge_muduru": r["bolge_muduru"],
            "net_ciro":     _safe_float(r["net_ciro"]),
            "hedef":        _safe_float(r["hedef"]),
            "doluluk":      round(dol_raw * 100, 1) if dol_raw is not None else None,
            "mdo":          round(mdo_raw * 100, 2) if mdo_raw is not None else None,
            "obf":          _safe_float(r["obf"]),
            "sepet":        _safe_float(r["sepet"]),
        }

    sorted_rows = sorted(rows, key=lambda r: _safe_float(r["hedef_orani"]) or 0, reverse=True)
    top5 = [_enrich(r) for r in sorted_rows[:5]]
    bot5 = [_enrich(r) for r in sorted_rows[-5:]][::-1]
    return top5, bot5


async def iade_matrisi_mg(session: AsyncSession, gun: date) -> list[dict[str, Any]]:
    """Mağaza iade matrisi — beden paterni (günlük).

    Returns: [{urun_kodu, urun_adi, renk, beden, iade_adet, satis_adet, iade_orani}] top 15
    """
    bas, son = _gun_aralik(gun)
    # Satış ve iade ayrı satırlarda — CTE ile önce birleştir, sonra oran hesapla.
    sql = text("""
        WITH gun_data AS (
            SELECT urun_kodu, MAX(urun_adi) AS urun_adi, renk, beden,
                   SUM(COALESCE(satis_adet, 0))        AS satis_adet,
                   SUM(ABS(COALESCE(iade_adeti, 0)))   AS iade_adet
            FROM incorta_magaza_gunluk
            WHERE tarih >= :bas AND tarih < :son
            GROUP BY urun_kodu, renk, beden
        )
        SELECT urun_kodu, urun_adi, renk, beden, iade_adet, satis_adet,
               ROUND(
                 (iade_adet / NULLIF(satis_adet, 0) * 100)::numeric, 1
               ) AS iade_orani
        FROM gun_data
        WHERE iade_adet > 0 AND urun_kodu IS NOT NULL
        ORDER BY iade_adet DESC
        LIMIT 15
    """)
    rows = (await session.execute(sql, {"bas": bas, "son": son})).mappings().all()
    return [dict(r) for r in rows]


async def lfl_ozet(session: AsyncSession, gun: date) -> dict[str, Any]:
    """Same-store sales — aylık YoY karşılaştırma.

    Returns:
        {mevcut: bool, bu_yil: float|None, gecen_yil: float|None, lfl_pct: float|None,
         ay: int, yil: int}
    """
    sql = text("""
        SELECT
            NULLIF(net_ciro, '--')::numeric AS net_ciro
        FROM incorta_magaza_performans
        WHERE yil = :yil AND ROUND(ay::numeric) = :ay
          AND bolge_muduru IS NULL AND magaza IS NULL
        LIMIT 1
    """)
    row_bu = (await session.execute(sql, {"yil": gun.year, "ay": gun.month})).mappings().one_or_none()
    row_gec = (await session.execute(sql, {"yil": gun.year - 1, "ay": gun.month})).mappings().one_or_none()

    bu_yil   = _safe_float(row_bu["net_ciro"])  if row_bu else None
    gec_yil  = _safe_float(row_gec["net_ciro"]) if row_gec else None
    lfl = (
        round((bu_yil - gec_yil) / abs(gec_yil) * 100, 1)
        if bu_yil and gec_yil else None
    )

    return {
        "mevcut":    bu_yil is not None,
        "bu_yil":    bu_yil,
        "gecen_yil": gec_yil,
        "lfl_pct":   lfl,
        "ay":        gun.month,
        "yil":       gun.year,
    }
