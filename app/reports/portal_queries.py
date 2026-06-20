"""SQL queries for the analytics portal.

All queries run on the read-only session and return plain dicts.
Multi-value filters (ay, kanal) use PostgreSQL ANY(:arr) binding.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_AY_ADI = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}

_RISK_SEVIYE = [
    (75, "KRİTİK"),
    (50, "YÜKSEK"),
    (25, "ORTA"),
    (10, "DÜŞÜK"),
    (0,  "SAĞLIKLI"),
]


def _risk_seviye(iade_pct: float) -> str:
    for threshold, label in _RISK_SEVIYE:
        if iade_pct >= threshold:
            return label
    return "SAĞLIKLI"


def _where(yil: Optional[int], aylar: List[int], kanallar: List[str],
           alias: str = "s") -> tuple:
    """Build dynamic WHERE clause + params dict for sales tables."""
    conditions = []
    params: Dict[str, Any] = {}
    if yil:
        conditions.append(f"{alias}.yil = :yil")
        params["yil"] = yil
    if aylar:
        conditions.append(f"{alias}.ay = ANY(:aylar)")
        params["aylar"] = aylar
    if kanallar:
        conditions.append(f"{alias}.satis_kanali = ANY(:kanallar)")
        params["kanallar"] = kanallar
    clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return clause, params


# ── Filter options ────────────────────────────────────────────────────────────

async def get_filters(session: AsyncSession) -> dict:
    rows = await session.execute(text(
        "SELECT DISTINCT yil FROM incorta_satis ORDER BY yil DESC"))
    yillar = [r[0] for r in rows]

    rows = await session.execute(text(
        "SELECT DISTINCT satis_kanali FROM incorta_satis ORDER BY satis_kanali"))
    kanallar = [r[0] for r in rows]

    rows = await session.execute(text(
        "SELECT DISTINCT renk FROM incorta_satis WHERE renk IS NOT NULL ORDER BY renk"))
    renkler = [r[0] for r in rows]

    rows = await session.execute(text(
        "SELECT DISTINCT beden FROM incorta_satis WHERE beden IS NOT NULL ORDER BY beden"))
    bedenler = [r[0] for r in rows]

    # PLM attribute filters (if pim_products table exists)
    markalar, sezonlar, urun_gruplari = [], [], []
    try:
        rows = await session.execute(text(
            "SELECT DISTINCT marka_adi FROM pim_products WHERE marka_adi IS NOT NULL ORDER BY marka_adi"))
        markalar = [r[0] for r in rows]

        rows = await session.execute(text(
            "SELECT DISTINCT sezon_adi FROM pim_products WHERE sezon_adi IS NOT NULL ORDER BY sezon_adi DESC LIMIT 20"))
        sezonlar = [r[0] for r in rows]

        rows = await session.execute(text(
            "SELECT DISTINCT urun_grubu_adi FROM pim_products WHERE urun_grubu_adi IS NOT NULL ORDER BY urun_grubu_adi"))
        urun_gruplari = [r[0] for r in rows]
    except Exception:  # noqa: BLE001 — table may not exist yet
        pass

    return {
        "yillar": yillar,
        "aylar": list(range(1, 13)),
        "kanallar": kanallar,
        "renkler": renkler,
        "bedenler": bedenler,
        "markalar": markalar,
        "sezonlar": sezonlar,
        "urun_gruplari": urun_gruplari,
    }


# ── KPI Summary ───────────────────────────────────────────────────────────────

async def get_kpis(
    session: AsyncSession,
    yil: Optional[int],
    aylar: List[int],
    kanallar: List[str],
) -> dict:
    where_s, params = _where(yil, aylar, kanallar, "s")
    where_d = where_s.replace("s.", "d.") if where_s else ""
    where_i = where_s.replace("s.", "i.") if where_s else ""

    row = (await session.execute(text(f"""
        SELECT
            COALESCE(SUM(s.tutar), 0)              AS brut_ciro,
            COALESCE(SUM(s.adet::int), 0)          AS brut_adet
        FROM incorta_satis s {where_s}
    """), params)).mappings().first()

    row_d = (await session.execute(text(f"""
        SELECT
            ABS(COALESCE(SUM(d.tutar), 0))         AS iade_ciro,
            ABS(COALESCE(SUM(d.adet::int), 0))     AS iade_adet
        FROM incorta_depo_iade d {where_d}
    """), params)).mappings().first()

    row_i = (await session.execute(text(f"""
        SELECT
            ABS(COALESCE(SUM(i.tutar), 0))         AS iptal_ciro,
            ABS(COALESCE(SUM(i.adet::int), 0))     AS iptal_adet
        FROM incorta_iptal_siparis i {where_i}
    """), params)).mappings().first()

    brut = float(row["brut_ciro"])
    iade = float(row_d["iade_ciro"])
    iptal = float(row_i["iptal_ciro"])
    net = brut - iade - iptal
    brut_adet = int(row["brut_adet"])
    iade_adet = int(row_d["iade_adet"])
    iptal_adet = int(row_i["iptal_adet"])
    net_adet = brut_adet - iade_adet - iptal_adet

    return {
        "brut_ciro": brut,
        "iade_ciro": iade,
        "iptal_ciro": iptal,
        "net_ciro": net,
        "brut_adet": brut_adet,
        "net_adet": max(net_adet, 0),
        "iade_oran": round(iade / brut * 100, 1) if brut else 0.0,
        "iptal_oran": round(iptal / brut * 100, 1) if brut else 0.0,
        "net_obf": round(net / max(net_adet, 1), 0),
    }


# ── Overview (trend + channel) ────────────────────────────────────────────────

async def get_overview(
    session: AsyncSession,
    yil: Optional[int],
    aylar: List[int],
    kanallar: List[str],
) -> dict:
    params: Dict[str, Any] = {}
    y_clause = "WHERE s.yil = :yil" if yil else ""
    if yil:
        params["yil"] = yil

    # Monthly trend (always full year for context)
    rows = (await session.execute(text(f"""
        SELECT s.ay,
               (SUM(s.tutar)/1000000)::numeric(10,2) AS brut_m,
               (ABS(SUM(COALESCE(d.tutar,0)))/1000000)::numeric(10,2) AS iade_m
        FROM incorta_satis s
        LEFT JOIN incorta_depo_iade d
               ON s.urun_kodu=d.urun_kodu AND s.ay=d.ay AND s.yil=d.yil
              AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        {y_clause}
        GROUP BY s.ay ORDER BY s.ay
    """), params)).mappings().all()

    trend = []
    for r in rows:
        brut = float(r["brut_m"])
        iade = float(r["iade_m"])
        trend.append({
            "ay": r["ay"],
            "ay_adi": _AY_ADI.get(r["ay"], str(r["ay"])),
            "brut_m": brut,
            "iade_m": iade,
            "net_m": round(brut - iade, 2),
        })

    # Channel breakdown
    where_s, params2 = _where(yil, aylar, kanallar, "s")
    where_d = where_s.replace("s.", "d.") if where_s else ""

    satis_rows = (await session.execute(text(f"""
        SELECT satis_kanali,
               SUM(tutar) AS ciro, SUM(adet::int) AS adet,
               (100.0*SUM(tutar)/SUM(SUM(tutar))OVER())::numeric(8,1) AS pay
        FROM incorta_satis s {where_s}
        GROUP BY satis_kanali ORDER BY SUM(tutar) DESC LIMIT 8
    """), params2)).mappings().all()

    iade_rows = (await session.execute(text(f"""
        SELECT satis_kanali,
               ABS(SUM(tutar)) AS iade_ciro, ABS(SUM(adet::int)) AS iade_adet,
               (100.0*ABS(SUM(tutar))/SUM(ABS(SUM(tutar)))OVER())::numeric(8,1) AS iade_pay
        FROM incorta_depo_iade d {where_d}
        GROUP BY satis_kanali ORDER BY ABS(SUM(tutar)) DESC
    """), params2)).mappings().all()

    iade_map = {r["satis_kanali"]: r for r in iade_rows}
    kanal = []
    for r in satis_rows:
        k = r["satis_kanali"]
        ir = iade_map.get(k, {})
        ciro = float(r["ciro"])
        iade_ciro = float(ir.get("iade_ciro", 0))
        kanal.append({
            "kanal": k,
            "ciro": ciro,
            "adet": int(r["adet"]),
            "pay": float(r["pay"]),
            "iade_ciro": iade_ciro,
            "iade_adet": int(ir.get("iade_adet", 0)),
            "iade_pay": float(ir.get("iade_pay", 0)),
            "iade_oran": round(iade_ciro / ciro * 100, 1) if ciro else 0.0,
        })

    return {"trend": trend, "kanal": kanal}


# ── Product list ──────────────────────────────────────────────────────────────

async def get_products(
    session: AsyncSession,
    yil: Optional[int],
    aylar: List[int],
    kanallar: List[str],
    urun_kodu: Optional[str],
    renk: Optional[str],
    beden: Optional[str],
    marka: Optional[str] = None,
    sezon: Optional[str] = None,
    urun_grubu: Optional[str] = None,
    sort_by: str = "risk_skoru",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    conditions: List[str] = []
    params: Dict[str, Any] = {}

    if yil:
        conditions.append("s.yil = :yil"); params["yil"] = yil
    if aylar:
        conditions.append("s.ay = ANY(:aylar)"); params["aylar"] = aylar
    if kanallar:
        conditions.append("s.satis_kanali = ANY(:kanallar)"); params["kanallar"] = kanallar
    if urun_kodu:
        conditions.append("(s.urun_kodu ILIKE :sku OR s.urun_adi ILIKE :sku)")
        params["sku"] = f"%{urun_kodu}%"
    if renk:
        conditions.append("s.renk = :renk"); params["renk"] = renk
    if beden:
        conditions.append("s.beden = :beden"); params["beden"] = beden

    # PLM attribute filters (applied via JOIN on pim_products)
    plm_conditions: List[str] = []
    if marka:
        plm_conditions.append("p.marka_adi = :marka"); params["marka"] = marka
    if sezon:
        plm_conditions.append("p.sezon_adi = :sezon"); params["sezon"] = sezon
    if urun_grubu:
        plm_conditions.append("p.urun_grubu_adi = :urun_grubu"); params["urun_grubu"] = urun_grubu

    cond_s = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    cond_d = cond_s.replace("s.", "d.") if cond_s else ""
    cond_i = cond_s.replace("s.", "i.") if cond_s else ""
    plm_having = (" AND " + " AND ".join(plm_conditions)) if plm_conditions else ""

    sort_map = {
        "risk_skoru": "risk_skoru DESC",
        "iade_pct":   "iade_pct DESC",
        "brut_ciro":  "brut_ciro DESC",
        "net_ciro":   "net_ciro DESC",
    }
    order = sort_map.get(sort_by, "risk_skoru DESC")

    # pim_products join clause (gracefully skipped if table not ready yet)
    plm_join = "LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu"
    plm_where = plm_having  # extra conditions that reference p.*

    base_sql = f"""
        WITH satis AS (
            SELECT urun_kodu, MAX(urun_adi) urun_adi,
                   SUM(tutar) brut_ciro, SUM(adet::int) brut_adet
            FROM incorta_satis s {cond_s}
            GROUP BY urun_kodu
        ),
        iade AS (
            SELECT urun_kodu,
                   ABS(SUM(tutar)) iade_ciro, ABS(SUM(adet::int)) iade_adet
            FROM incorta_depo_iade d {cond_d}
            GROUP BY urun_kodu
        ),
        iptal AS (
            SELECT urun_kodu,
                   ABS(SUM(tutar)) iptal_ciro, ABS(SUM(adet::int)) iptal_adet
            FROM incorta_iptal_siparis i {cond_i}
            GROUP BY urun_kodu
        ),
        combined AS (
            SELECT
                s.urun_kodu, s.urun_adi,
                s.brut_ciro, s.brut_adet,
                COALESCE(i.iade_ciro, 0)   AS iade_ciro,
                COALESCE(i.iade_adet, 0)   AS iade_adet,
                COALESCE(ip.iptal_ciro, 0) AS iptal_ciro,
                s.brut_ciro - COALESCE(i.iade_ciro,0) - COALESCE(ip.iptal_ciro,0) AS net_ciro,
                (COALESCE(i.iade_ciro,0) / NULLIF(s.brut_ciro,0) * 100)::numeric(8,1) AS iade_pct,
                (COALESCE(ip.iptal_ciro,0) / NULLIF(s.brut_ciro,0) * 100)::numeric(8,1) AS iptal_pct,
                ((COALESCE(i.iade_ciro,0)/NULLIF(s.brut_ciro,0)*60)
                 + (COALESCE(ip.iptal_ciro,0)/NULLIF(s.brut_ciro,0)*40))::numeric(8,1) AS risk_skoru,
                -- PLM attributes (NULL if pim_products not yet available)
                p.marka_adi, p.sezon_adi, p.urun_grubu_adi, p.ana_grup_adi,
                p.first_color_code,
                -- Image: prefer pim_products URL; fallback via pim_colors + incorta renk
                p.default_image_url AS default_image_url
            FROM satis s
            LEFT JOIN iade  i  ON s.urun_kodu = i.urun_kodu
            LEFT JOIN iptal ip ON s.urun_kodu = ip.urun_kodu
            {plm_join}
            WHERE 1=1 {plm_where}
        )
    """

    total_row = (await session.execute(text(base_sql + "SELECT COUNT(*) FROM combined"), params)).scalar()
    total = int(total_row or 0)

    params["limit"] = page_size
    params["offset"] = (page - 1) * page_size

    rows = (await session.execute(text(
        base_sql + f"SELECT * FROM combined ORDER BY {order} LIMIT :limit OFFSET :offset"
    ), params)).mappings().all()

    items = []
    for r in rows:
        iade_pct = float(r["iade_pct"] or 0)
        # Use default_image_url from pim_products (set by McpConnector parser)
        img = r.get("default_image_url")
        items.append({
            "urun_kodu": r["urun_kodu"],
            "urun_adi": r["urun_adi"],
            "brut_ciro": float(r["brut_ciro"]),
            "net_ciro": float(r["net_ciro"]),
            "iade_ciro": float(r["iade_ciro"]),
            "brut_adet": int(r["brut_adet"]),
            "iade_adet": int(r["iade_adet"]),
            "iade_pct": iade_pct,
            "risk_skoru": float(r["risk_skoru"] or 0),
            "risk_seviye": _risk_seviye(iade_pct),
            "marka_adi": r.get("marka_adi"),
            "sezon_adi": r.get("sezon_adi"),
            "urun_grubu_adi": r.get("urun_grubu_adi"),
            "ana_grup_adi": r.get("ana_grup_adi"),
            "image_url": img,
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


# ── Product drilldown ─────────────────────────────────────────────────────────

async def get_product_drilldown(
    session: AsyncSession,
    urun_kodu: str,
    yil: Optional[int],
    aylar: List[int],
) -> Optional[dict]:
    params: Dict[str, Any] = {"sku": urun_kodu}
    y_clause = "AND s.yil = :yil" if yil else ""
    a_clause = "AND s.ay = ANY(:aylar)" if aylar else ""
    if yil:
        params["yil"] = yil
    if aylar:
        params["aylar"] = aylar

    # Use explicit alias in all single-table queries too
    s_y = "AND s.yil = :yil" if yil else ""
    s_a = "AND s.ay = ANY(:aylar)" if aylar else ""
    d_y = "AND d.yil = :yil" if yil else ""
    d_a = "AND d.ay = ANY(:aylar)" if aylar else ""

    # Summary
    summary = (await session.execute(text(f"""
        SELECT s.urun_kodu, MAX(s.urun_adi) urun_adi,
               SUM(s.tutar) brut_ciro, SUM(s.adet::int) brut_adet
        FROM incorta_satis s
        WHERE s.urun_kodu = :sku {s_y} {s_a}
        GROUP BY s.urun_kodu
    """), params)).mappings().first()

    if not summary:
        return None

    iade_sum = (await session.execute(text(f"""
        SELECT ABS(SUM(d.tutar)) iade_ciro FROM incorta_depo_iade d
        WHERE d.urun_kodu = :sku {d_y} {d_a}
    """), params)).mappings().first()
    # Not: iade ay filtresi satış ay filtresi ile aynı — tutarlı dönem karşılaştırması

    brut = float(summary["brut_ciro"])
    iade = float(iade_sum["iade_ciro"] or 0) if iade_sum else 0.0
    iade_pct = round(iade / brut * 100, 1) if brut else 0.0

    # Monthly trend — join on matching month to compare same-period sales/returns
    yil_join = "AND d.yil = s.yil" if yil else ""
    ay_rows = (await session.execute(text(f"""
        SELECT s.ay,
               SUM(s.tutar) brut, SUM(s.adet::int) brut_adet,
               ABS(COALESCE(SUM(d.tutar), 0)) iade,
               ABS(COALESCE(SUM(d.adet::int), 0)) iade_adet
        FROM incorta_satis s
        LEFT JOIN incorta_depo_iade d
               ON d.urun_kodu = s.urun_kodu AND d.ay = s.ay {yil_join}
              AND d.satis_kanali = s.satis_kanali AND d.renk = s.renk AND d.beden = s.beden
        WHERE s.urun_kodu = :sku {s_y} {s_a}
        GROUP BY s.ay ORDER BY s.ay
    """), params)).mappings().all()

    aylik_trend = []
    for r in ay_rows:
        b = float(r["brut"])
        i = float(r["iade"])
        aylik_trend.append({
            "ay": r["ay"], "ay_adi": _AY_ADI.get(r["ay"], str(r["ay"])),
            "brut_ciro": b, "iade_ciro": i,
            "brut_adet": int(r["brut_adet"]), "iade_adet": int(r["iade_adet"]),
            "iade_pct": round(i / b * 100, 1) if b else 0.0,
        })

    # Allowed column names for dağılım queries (whitelist — no injection risk)
    _SAFE_COLS = {"renk", "beden", "satis_kanali"}

    async def _dagil(col: str) -> list:
        if col not in _SAFE_COLS:
            return []
        rows = (await session.execute(text(f"""
            SELECT s.{col} deger,
                   SUM(s.tutar) brut, SUM(s.adet::int) brut_adet,
                   ABS(COALESCE(SUM(d.tutar),0)) iade,
                   ABS(COALESCE(SUM(d.adet::int),0)) iade_adet
            FROM incorta_satis s
            LEFT JOIN incorta_depo_iade d
                   ON d.urun_kodu = s.urun_kodu AND d.{col} = s.{col} {yil_join}
                  AND d.satis_kanali = s.satis_kanali
            WHERE s.urun_kodu = :sku AND s.{col} IS NOT NULL {s_y} {s_a}
            GROUP BY s.{col} ORDER BY SUM(s.tutar) DESC LIMIT 15
        """), params)).mappings().all()
        result = []
        for r in rows:
            b = float(r["brut"])
            i = float(r["iade"])
            result.append({
                "deger": str(r["deger"] or ""),
                "brut_ciro": b, "iade_ciro": i,
                "adet": int(r["brut_adet"]),
                "iade_pct": round(i / b * 100, 1) if b else 0.0,
            })
        return result

    # Fetch PLM attributes if available
    plm = {}
    try:
        plm_row = (await session.execute(text("""
            SELECT marka_adi, sezon_adi, urun_grubu_adi, ana_grup_adi,
                   first_color_code, color_codes
            FROM pim_products WHERE urun_kodu = :sku LIMIT 1
        """), {"sku": urun_kodu})).mappings().first()
        if plm_row:
            plm = dict(plm_row)
    except Exception:  # noqa: BLE001
        pass

    renk_dag = await _dagil("renk")
    beden_dag = await _dagil("beden")
    kanal_dag = await _dagil("satis_kanali")

    return {
        "urun_kodu": urun_kodu,
        "urun_adi": summary["urun_adi"],
        "brut_ciro": brut,
        "net_ciro": brut - iade,
        "iade_pct": iade_pct,
        "risk_seviye": _risk_seviye(iade_pct),
        "aylik_trend": aylik_trend,
        "renk_dagilim": renk_dag,
        "beden_dagilim": beden_dag,
        "kanal_dagilim": kanal_dag,
        # PLM attributes
        "marka_adi": plm.get("marka_adi"),
        "sezon_adi": plm.get("sezon_adi"),
        "urun_grubu_adi": plm.get("urun_grubu_adi"),
        "ana_grup_adi": plm.get("ana_grup_adi"),
        "first_color_code": plm.get("first_color_code"),
        "color_codes": plm.get("color_codes") or
                       ",".join(sorted({r["deger"] for r in renk_dag if r["deger"]})),
    }


# ── Color analysis ────────────────────────────────────────────────────────────

async def get_colors(
    session: AsyncSession,
    yil: Optional[int],
    aylar: List[int],
    kanallar: List[str],
) -> list:
    where_s, params = _where(yil, aylar, kanallar, "s")
    where_d = where_s.replace("s.", "d.") if where_s else ""

    renk_s = "AND" if where_s else "WHERE"
    renk_d = "AND" if where_d else "WHERE"
    rows = (await session.execute(text(f"""
        WITH satis AS (
            SELECT renk, SUM(tutar) brut, SUM(adet::int) brut_adet
            FROM incorta_satis s {where_s}
            {renk_s} renk IS NOT NULL GROUP BY renk
        ),
        iade AS (
            SELECT renk, ABS(SUM(tutar)) iade, ABS(SUM(adet::int)) iade_adet
            FROM incorta_depo_iade d {where_d}
            {renk_d} renk IS NOT NULL GROUP BY renk
        )
        SELECT s.renk,
               s.brut, COALESCE(i.iade,0) iade,
               s.brut_adet, COALESCE(i.iade_adet,0) iade_adet,
               (100.0*s.brut/SUM(s.brut)OVER())::numeric(8,1) pay,
               (COALESCE(i.iade,0)/NULLIF(s.brut,0)*100)::numeric(8,1) iade_pct
        FROM satis s LEFT JOIN iade i ON s.renk=i.renk
        ORDER BY s.brut DESC LIMIT 30
    """), params)).mappings().all()

    return [
        {
            "renk": r["renk"],
            "brut_ciro": float(r["brut"]),
            "iade_ciro": float(r["iade"]),
            "brut_adet": int(r["brut_adet"]),
            "iade_adet": int(r["iade_adet"]),
            "iade_pct": float(r["iade_pct"] or 0),
            "pay": float(r["pay"]),
        }
        for r in rows
    ]

# ── Dönem otomatik tespiti ────────────────────────────────────────────────────

async def get_latest_period(session: AsyncSession) -> dict:
    """Son tam ay ve yılı tespit et (ciro > 10M eşiği)."""
    rows = (await session.execute(text("""
        SELECT yil, ay, SUM(tutar)/1e6 AS ciro_m
        FROM incorta_satis
        GROUP BY yil, ay
        ORDER BY yil DESC, ay DESC
        LIMIT 12
    """))).mappings().all()

    for r in rows:
        if float(r["ciro_m"]) >= 10.0:  # en az 10M TL = tam ay
            return {"yil": int(r["yil"]), "ay": int(r["ay"]), "ciro_m": float(r["ciro_m"])}
    # fallback
    if rows:
        return {"yil": int(rows[0]["yil"]), "ay": int(rows[0]["ay"]), "ciro_m": float(rows[0]["ciro_m"])}
    return {"yil": 2026, "ay": 5, "ciro_m": 0.0}


# ── Executive Summary ─────────────────────────────────────────────────────────

async def get_exec_summary(
    session: AsyncSession,
    yil: Optional[int],
    aylar: List[int],
    kanallar: List[str],
) -> dict:
    """Zenginleştirilmiş Executive Summary: KPI + MoM + kategori + ürün + insights."""
    kpis = await get_kpis(session, yil=yil, aylar=aylar, kanallar=kanallar)
    kanal_data = await get_overview(session, yil=yil, aylar=aylar, kanallar=kanallar)
    kanal_list = kanal_data.get("kanal", [])

    # ── MoM Karşılaştırma ──────────────────────────────────────────────────────
    mom = {}
    if yil and len(aylar) == 1:
        prev_ay = aylar[0] - 1
        prev_yil = yil
        if prev_ay == 0:
            prev_ay = 12; prev_yil = yil - 1
        try:
            prev_kpis = await get_kpis(session, yil=prev_yil, aylar=[prev_ay], kanallar=kanallar)
            def _chg(curr, prev):
                if not prev: return None
                return round((curr - prev) / prev * 100, 1)
            mom = {
                "brut_ciro_chg": _chg(kpis["brut_ciro"], prev_kpis["brut_ciro"]),
                "net_ciro_chg":  _chg(kpis["net_ciro"],  prev_kpis["net_ciro"]),
                "iade_oran_chg": round(kpis["iade_oran"] - prev_kpis["iade_oran"], 1),
                "prev_ay": prev_ay, "prev_yil": prev_yil,
                "prev_brut": prev_kpis["brut_ciro"],
                "prev_net":  prev_kpis["net_ciro"],
            }
        except Exception:
            pass

    # ── Top 3 Ürün (net ciro) ──────────────────────────────────────────────────
    where_s, params_u = _where(yil, aylar, kanallar, "s")
    where_d = where_s.replace("s.", "d.") if where_s else ""
    top_urun_rows = (await session.execute(text(f"""
        WITH sat AS (SELECT urun_kodu, MAX(urun_adi) urun_adi, SUM(tutar) brut FROM incorta_satis s {where_s} GROUP BY urun_kodu),
             iad AS (SELECT urun_kodu, ABS(SUM(tutar)) iade FROM incorta_depo_iade d {where_d} GROUP BY urun_kodu)
        SELECT s.urun_kodu, s.urun_adi, s.brut - COALESCE(i.iade,0) AS net_ciro,
               (COALESCE(i.iade,0)/NULLIF(s.brut,0)*100)::numeric(5,1) AS iade_pct
        FROM sat s LEFT JOIN iad i ON s.urun_kodu=i.urun_kodu
        ORDER BY net_ciro DESC LIMIT 3
    """), params_u)).mappings().all()

    # ── En Riskli 3 Ürün ──────────────────────────────────────────────────────
    risk_urun_rows = (await session.execute(text(f"""
        WITH sat AS (SELECT urun_kodu, MAX(urun_adi) urun_adi, SUM(tutar) brut FROM incorta_satis s {where_s} GROUP BY urun_kodu),
             iad AS (SELECT urun_kodu, ABS(SUM(tutar)) iade FROM incorta_depo_iade d {where_d} GROUP BY urun_kodu)
        SELECT s.urun_kodu, s.urun_adi, s.brut AS brut_ciro,
               (COALESCE(i.iade,0)/NULLIF(s.brut,0)*100)::numeric(5,1) AS iade_pct
        FROM sat s LEFT JOIN iad i ON s.urun_kodu=i.urun_kodu
        WHERE s.brut > 50000
        ORDER BY iade_pct DESC LIMIT 3
    """), params_u)).mappings().all()

    # ── Kategori özeti ────────────────────────────────────────────────────────
    kat_rows = (await session.execute(text(f"""
        WITH sat AS (SELECT s.urun_kodu, SUM(s.tutar) brut FROM incorta_satis s {where_s} GROUP BY s.urun_kodu),
             iad AS (SELECT urun_kodu, ABS(SUM(tutar)) iade FROM incorta_depo_iade d {where_d} GROUP BY urun_kodu)
        SELECT COALESCE(p.urun_grubu_adi,'Diğer') AS kat,
               SUM(sat.brut) AS brut, ABS(COALESCE(SUM(iad.iade),0)) AS iade,
               (ABS(COALESCE(SUM(iad.iade),0))/NULLIF(SUM(sat.brut),0)*100)::numeric(5,1) AS iade_pct
        FROM sat
        LEFT JOIN iad ON sat.urun_kodu=iad.urun_kodu
        LEFT JOIN pim_products p ON p.urun_kodu=sat.urun_kodu
        GROUP BY COALESCE(p.urun_grubu_adi,'Diğer')
        ORDER BY SUM(sat.brut) DESC LIMIT 5
    """), params_u)).mappings().all()

    # ── Alert / Fırsat / Aksiyon üretimi ──────────────────────────────────────
    alerts, opportunities, actions = [], [], []
    iade_oran  = kpis["iade_oran"]
    iptal_oran = kpis["iptal_oran"]

    # Temel iade uyarıları
    if iade_oran > 35:
        alerts.append({"level": "crit", "icon": "🔴",
            "title": f"İade Oranı Kritik — %{iade_oran}",
            "desc": f"{_fmt_m(kpis['iade_ciro'])} brüt ciro eriyor. Her {round(100/iade_oran,1):.1f} satıştan 1'i iade."})
    elif iade_oran > 25:
        alerts.append({"level": "warn", "icon": "🟡",
            "title": f"İade Oranı Yüksek — %{iade_oran}",
            "desc": f"{_fmt_m(kpis['iade_ciro'])} iade kaybı. Önceki aylarla karşılaştırın."})
    if iptal_oran > 8:
        alerts.append({"level": "crit", "icon": "🔴",
            "title": f"İptal Oranı Yüksek — %{iptal_oran}",
            "desc": f"{_fmt_m(kpis['iptal_ciro'])} iptal kaybı. Ödeme akışı incelenmeli."})

    # MoM düşüş uyarıları
    if mom.get("net_ciro_chg") is not None:
        if mom["net_ciro_chg"] < -15:
            alerts.append({"level": "crit", "icon": "📉",
                "title": f"Net Ciro MoM Düşüş — %{abs(mom['net_ciro_chg'])}",
                "desc": f"{_AY_ADI.get(mom['prev_ay'],'')} ile kıyasla {_fmt_m(abs(kpis['net_ciro']-mom['prev_net']))} azaldı."})
        elif mom.get("iade_oran_chg", 0) > 3:
            alerts.append({"level": "warn", "icon": "📊",
                "title": f"İade Oranı MoM Artış — +%{mom['iade_oran_chg']}",
                "desc": f"Bir önceki aya göre iade oranı {mom['iade_oran_chg']} puan arttı."})

    # Kanal riskleri
    if kanal_list:
        top = kanal_list[0]
        if top["pay"] > 60:
            alerts.append({"level": "warn", "icon": "⚠️",
                "title": f"{top['kanal']} Bağımlılığı — %{top['pay']}",
                "desc": "Gelirin %60'ı tek kanaldan geliyor. Çeşitlendirme riski azaltır."})
        worst_iade = max(kanal_list, key=lambda k: k.get("iade_oran", 0))
        if worst_iade.get("iade_oran", 0) > 40:
            alerts.append({"level": "warn", "icon": "🔁",
                "title": f"{worst_iade['kanal']} — Yüksek İade %{worst_iade['iade_oran']}",
                "desc": f"Bu kanalda {_fmt_m(worst_iade['iade_ciro'])} iade. Kanal özelinde analiz gerekiyor."})

    # Riskli ürün uyarısı
    if risk_urun_rows and float(risk_urun_rows[0]["iade_pct"] or 0) > 60:
        r = risk_urun_rows[0]
        alerts.append({"level": "crit", "icon": "📦",
            "title": f"Kritik SKU: {r['urun_adi'][:30]}",
            "desc": f"%{r['iade_pct']} iade · {_fmt_m(float(r['brut_ciro']))} brüt ciro. Acil aksiyon gerekiyor."})

    # Kategori riski
    risky_kat = [k for k in kat_rows if float(k["iade_pct"] or 0) > 35]
    for rk in risky_kat[:2]:
        alerts.append({"level": "warn", "icon": "🗂️",
            "title": f"{rk['kat']} — %{rk['iade_pct']} İade",
            "desc": f"{_fmt_m(float(rk['iade']))} iade kaybı. Kategori bazında önlem alınmalı."})

    # Fırsatlar
    if mom.get("net_ciro_chg") and mom["net_ciro_chg"] > 10:
        opportunities.append({"level": "good", "icon": "📈",
            "title": f"Net Ciro MoM Büyüme — +%{mom['net_ciro_chg']}",
            "desc": f"Önceki aya göre {_fmt_m(kpis['net_ciro']-mom['prev_net'])} artış. Pozitif momentum."})

    healthy_kanals = [k for k in kanal_list if k.get("iade_oran", 100) < 20 and k["pay"] < 25]
    for h in healthy_kanals[:2]:
        opportunities.append({"level": "good", "icon": "🚀",
            "title": f"{h['kanal']} — Büyüme Fırsatı",
            "desc": f"%{h['iade_oran']} düşük iade · %{h['pay']} pay. Yatırım artışı değerlendirilmeli."})

    healthy_kat = [k for k in kat_rows if float(k["iade_pct"] or 0) < 20 and float(k["brut"] or 0) > 1e6]
    for hk in healthy_kat[:2]:
        opportunities.append({"level": "good", "icon": "💎",
            "title": f"{hk['kat']} — Sağlıklı Kategori",
            "desc": f"%{hk['iade_pct']} iade ile güvenli büyüme alanı. {_fmt_m(float(hk['brut']))} ciro."})

    if top_urun_rows:
        t = top_urun_rows[0]
        opportunities.append({"level": "good", "icon": "⭐",
            "title": f"En İyi Ürün: {t['urun_adi'][:30]}",
            "desc": f"{_fmt_m(float(t['net_ciro']))} net ciro · %{t['iade_pct']} iade. Stok büyütme fırsatı."})

    # Aksiyonlar
    action_num = 1
    if iade_oran > 25:
        actions.append({"num": action_num, "title": "İade Kök Neden Analizi",
            "desc": f"%{iade_oran} iade · {_fmt_m(kpis['iade_ciro'])} kayıp → İade Analizi sekmesi"})
        action_num += 1
    if risk_urun_rows:
        r = risk_urun_rows[0]
        actions.append({"num": action_num, "title": f"Kritik SKU İncele: {r['urun_adi'][:25]}",
            "desc": f"%{r['iade_pct']} iade · SKU: {r['urun_kodu']}"})
        action_num += 1
    if kanal_list:
        worst = max(kanal_list, key=lambda k: k.get("iade_oran", 0))
        actions.append({"num": action_num, "title": f"{worst['kanal']} Kanal Optimizasyonu",
            "desc": f"%{worst.get('iade_oran',0)} iade · {_fmt_m(worst.get('iade_ciro',0))} kayıp"})
        action_num += 1
    if mom.get("net_ciro_chg") and mom["net_ciro_chg"] < -10:
        actions.append({"num": action_num, "title": "Ciro Düşüş Analizi",
            "desc": f"MoM %{abs(mom['net_ciro_chg'])} düşüş · {_AY_ADI.get(mom['prev_ay'],'')} karşılaştırması"})

    return {
        "kpis": kpis,
        "mom": mom,
        "alerts": alerts,
        "opportunities": opportunities,
        "actions": actions,
        "kanal": kanal_list[:5],
        "top_urunler": [{"urun_kodu": r["urun_kodu"], "urun_adi": r["urun_adi"],
                          "net_ciro": float(r["net_ciro"]), "iade_pct": float(r["iade_pct"] or 0)} for r in top_urun_rows],
        "risk_urunler": [{"urun_kodu": r["urun_kodu"], "urun_adi": r["urun_adi"],
                           "brut_ciro": float(r["brut_ciro"]), "iade_pct": float(r["iade_pct"] or 0)} for r in risk_urun_rows],
        "kategoriler": [{"kat": r["kat"], "brut": float(r["brut"]), "iade": float(r["iade"]),
                          "iade_pct": float(r["iade_pct"] or 0)} for r in kat_rows],
        "kanal": kanal_list[:5],
    }


def _fmt_m(val: float) -> str:
    if abs(val) >= 1e6: return f"{val/1e6:.1f}M ₺"
    if abs(val) >= 1e3: return f"{val/1e3:.0f}K ₺"
    return f"{val:.0f} ₺"


# ── Kategori (ürün grubu) analizi ─────────────────────────────────────────────

async def get_kategori(
    session: AsyncSession,
    yil: Optional[int],
    aylar: List[int],
    kanallar: List[str],
) -> list:
    """Ürün grubu bazında brüt/net/iade breakdown — pim_products join."""
    where_s, params = _where(yil, aylar, kanallar, "s")
    where_d = where_s.replace("s.", "d.") if where_s else ""

    rows = (await session.execute(text(f"""
        WITH satis AS (
            SELECT s.urun_kodu, SUM(s.tutar) brut, SUM(s.adet::int) brut_adet
            FROM incorta_satis s {where_s}
            GROUP BY s.urun_kodu
        ),
        iade AS (
            SELECT d.urun_kodu, ABS(SUM(d.tutar)) iade FROM incorta_depo_iade d {where_d}
            GROUP BY d.urun_kodu
        )
        SELECT
            COALESCE(p.urun_grubu_adi, 'Diğer') AS kategori,
            COALESCE(p.ana_grup_adi, 'Diğer') AS ana_grup,
            SUM(s.brut) AS brut_ciro,
            SUM(s.brut_adet) AS brut_adet,
            ABS(COALESCE(SUM(i.iade), 0)) AS iade_ciro,
            (100.0*SUM(s.brut)/SUM(SUM(s.brut))OVER())::numeric(6,1) AS pay,
            (ABS(COALESCE(SUM(i.iade),0))/NULLIF(SUM(s.brut),0)*100)::numeric(6,1) AS iade_pct
        FROM satis s
        LEFT JOIN iade i ON s.urun_kodu = i.urun_kodu
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        GROUP BY COALESCE(p.urun_grubu_adi,'Diğer'), COALESCE(p.ana_grup_adi,'Diğer')
        ORDER BY SUM(s.brut) DESC
        LIMIT 20
    """), params)).mappings().all()

    return [
        {
            "kategori": r["kategori"],
            "ana_grup": r["ana_grup"],
            "brut_ciro": float(r["brut_ciro"]),
            "brut_adet": int(r["brut_adet"]),
            "iade_ciro": float(r["iade_ciro"]),
            "net_ciro": float(r["brut_ciro"]) - float(r["iade_ciro"]),
            "pay": float(r["pay"]),
            "iade_pct": float(r["iade_pct"] or 0),
        }
        for r in rows
    ]


# ── Top ürünler (with images) ─────────────────────────────────────────────────

async def get_top_urunler(
    session: AsyncSession,
    yil: Optional[int],
    aylar: List[int],
    kanallar: List[str],
    limit: int = 10,
    sort_by: str = "net_ciro",
) -> list:
    """Top N ürün — görsel URL, marka, grup, iade oranı."""
    where_s, params = _where(yil, aylar, kanallar, "s")
    where_d = where_s.replace("s.", "d.") if where_s else ""
    params["lim"] = limit

    order = "net_ciro DESC" if sort_by == "net_ciro" else "iade_pct DESC"

    rows = (await session.execute(text(f"""
        WITH satis AS (
            SELECT urun_kodu, MAX(urun_adi) urun_adi,
                   SUM(tutar) brut, SUM(adet::int) brut_adet
            FROM incorta_satis s {where_s}
            GROUP BY urun_kodu
        ),
        iade AS (
            SELECT urun_kodu, ABS(SUM(tutar)) iade, ABS(SUM(adet::int)) iade_adet
            FROM incorta_depo_iade d {where_d}
            GROUP BY urun_kodu
        )
        SELECT
            s.urun_kodu, s.urun_adi,
            s.brut AS brut_ciro, s.brut_adet,
            COALESCE(i.iade, 0) AS iade_ciro,
            COALESCE(i.iade_adet, 0) AS iade_adet,
            s.brut - COALESCE(i.iade, 0) AS net_ciro,
            (COALESCE(i.iade,0)/NULLIF(s.brut,0)*100)::numeric(6,1) AS iade_pct,
            p.marka_adi, p.urun_grubu_adi, p.sezon_adi,
            p.default_image_url AS image_url
        FROM satis s
        LEFT JOIN iade i ON s.urun_kodu = i.urun_kodu
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        ORDER BY {order}
        LIMIT :lim
    """), params)).mappings().all()

    return [
        {
            "rank": idx + 1,
            "urun_kodu": r["urun_kodu"],
            "urun_adi": r["urun_adi"],
            "brut_ciro": float(r["brut_ciro"]),
            "net_ciro": float(r["net_ciro"]),
            "iade_ciro": float(r["iade_ciro"]),
            "brut_adet": int(r["brut_adet"]),
            "iade_adet": int(r["iade_adet"]),
            "iade_pct": float(r["iade_pct"] or 0),
            "marka_adi": r.get("marka_adi"),
            "urun_grubu_adi": r.get("urun_grubu_adi"),
            "sezon_adi": r.get("sezon_adi"),
            "image_url": r.get("image_url"),
        }
        for idx, r in enumerate(rows)
    ]


# ── İade analizi — beden + renk breakdown ─────────────────────────────────────

async def get_iade_analiz(
    session: AsyncSession,
    yil: Optional[int],
    aylar: List[int],
    kanallar: List[str],
) -> dict:
    """İade breakdown: beden, renk, kanal bazında."""
    where_s, params = _where(yil, aylar, kanallar, "s")
    where_d = where_s.replace("s.", "d.") if where_s else ""

    # WHERE koşullarını temiz birleştir
    def _add(clause: str, extra: str) -> str:
        return (clause + " AND " + extra) if clause else ("WHERE " + extra)

    ws_beden = _add(where_s, "s.beden IS NOT NULL")
    wd_beden = _add(where_d, "d.beden IS NOT NULL")
    ws_renk  = _add(where_s, "s.renk IS NOT NULL")
    wd_renk  = _add(where_d, "d.renk IS NOT NULL")

    # Beden bazında iade
    beden_rows = (await session.execute(text(f"""
        WITH sat AS (
            SELECT beden, SUM(tutar) brut, SUM(adet::int) brut_adet
            FROM incorta_satis s {ws_beden}
            GROUP BY beden
        ),
        iad AS (
            SELECT beden, ABS(SUM(tutar)) iade, ABS(SUM(adet::int)) iade_adet
            FROM incorta_depo_iade d {wd_beden}
            GROUP BY beden
        )
        SELECT sat.beden,
               sat.brut, COALESCE(iad.iade,0) iade,
               sat.brut_adet, COALESCE(iad.iade_adet,0) iade_adet,
               (COALESCE(iad.iade,0)/NULLIF(sat.brut,0)*100)::numeric(6,1) iade_pct
        FROM sat LEFT JOIN iad ON sat.beden=iad.beden
        ORDER BY sat.brut DESC LIMIT 20
    """), params)).mappings().all()

    # Renk bazında iade
    renk_rows = (await session.execute(text(f"""
        WITH sat AS (
            SELECT renk, SUM(tutar) brut, SUM(adet::int) brut_adet
            FROM incorta_satis s {ws_renk}
            GROUP BY renk
        ),
        iad AS (
            SELECT renk, ABS(SUM(tutar)) iade, ABS(SUM(adet::int)) iade_adet
            FROM incorta_depo_iade d {wd_renk}
            GROUP BY renk
        )
        SELECT sat.renk,
               sat.brut, COALESCE(iad.iade,0) iade,
               sat.brut_adet, COALESCE(iad.iade_adet,0) iade_adet,
               (COALESCE(iad.iade,0)/NULLIF(sat.brut,0)*100)::numeric(6,1) iade_pct
        FROM sat LEFT JOIN iad ON sat.renk=iad.renk
        ORDER BY COALESCE(iad.iade,0) DESC LIMIT 20
    """), params)).mappings().all()

    def _rows(rs):
        return [{"deger": r[0], "brut_ciro": float(r[1]), "iade_ciro": float(r[2]),
                 "brut_adet": int(r[3]), "iade_adet": int(r[4]),
                 "iade_pct": float(r[5] or 0)} for r in rs]

    return {
        "beden": [{"deger": r["beden"], "brut_ciro": float(r["brut"]),
                   "iade_ciro": float(r["iade"]), "brut_adet": int(r["brut_adet"]),
                   "iade_adet": int(r["iade_adet"]), "iade_pct": float(r["iade_pct"] or 0)}
                  for r in beden_rows],
        "renk": [{"deger": r["renk"], "brut_ciro": float(r["brut"]),
                  "iade_ciro": float(r["iade"]), "brut_adet": int(r["brut_adet"]),
                  "iade_adet": int(r["iade_adet"]), "iade_pct": float(r["iade_pct"] or 0)}
                 for r in renk_rows],
    }


# ── Kârlılık dashboard ────────────────────────────────────────────────────────

async def get_karlilik(
    session: AsyncSession,
    yil: Optional[int],
    aylar: List[int],
    kanallar: List[str],
) -> dict:
    """Risk segmentleri: kritik iade, yüksek risk, sağlıklı ürünler."""
    where_s, params = _where(yil, aylar, kanallar, "s")
    where_d = where_s.replace("s.", "d.") if where_s else ""
    where_i = where_s.replace("s.", "i.") if where_s else ""

    base = f"""
        WITH satis AS (
            SELECT urun_kodu, MAX(urun_adi) urun_adi,
                   SUM(tutar) brut, SUM(adet::int) brut_adet
            FROM incorta_satis s {where_s}
            GROUP BY urun_kodu
        ),
        iade AS (
            SELECT urun_kodu, ABS(SUM(tutar)) iade, ABS(SUM(adet::int)) iade_adet
            FROM incorta_depo_iade d {where_d} GROUP BY urun_kodu
        ),
        combined AS (
            SELECT s.urun_kodu, s.urun_adi,
                   s.brut AS brut_ciro, s.brut_adet,
                   COALESCE(i.iade,0) AS iade_ciro,
                   COALESCE(i.iade_adet,0) AS iade_adet,
                   s.brut - COALESCE(i.iade,0) AS net_ciro,
                   (COALESCE(i.iade,0)/NULLIF(s.brut,0)*100)::numeric(6,1) AS iade_pct,
                   p.marka_adi, p.urun_grubu_adi,
                   p.default_image_url AS image_url
            FROM satis s
            LEFT JOIN iade i ON s.urun_kodu = i.urun_kodu
            LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        )
    """

    def _build(rows) -> list:
        return [{"urun_kodu": r["urun_kodu"], "urun_adi": r["urun_adi"],
                 "brut_ciro": float(r["brut_ciro"]), "net_ciro": float(r["net_ciro"]),
                 "iade_ciro": float(r["iade_ciro"]), "brut_adet": int(r["brut_adet"]),
                 "iade_adet": int(r["iade_adet"]), "iade_pct": float(r["iade_pct"] or 0),
                 "marka_adi": r.get("marka_adi"), "urun_grubu_adi": r.get("urun_grubu_adi"),
                 "image_url": r.get("image_url")} for r in rows]

    kritik = _build((await session.execute(text(
        base + "SELECT * FROM combined WHERE brut_ciro > 50000 AND iade_pct >= 50 ORDER BY iade_ciro DESC LIMIT 15"
    ), params)).mappings().all())

    yuksek = _build((await session.execute(text(
        base + "SELECT * FROM combined WHERE brut_ciro > 50000 AND iade_pct >= 30 AND iade_pct < 50 ORDER BY iade_ciro DESC LIMIT 15"
    ), params)).mappings().all())

    saglikli = _build((await session.execute(text(
        base + "SELECT * FROM combined WHERE brut_adet >= 20 AND iade_pct < 15 ORDER BY net_ciro DESC LIMIT 15"
    ), params)).mappings().all())

    # Summary stats
    stats = (await session.execute(text(
        base + """
        SELECT
            COUNT(*) FILTER (WHERE iade_pct >= 50 AND brut_ciro > 50000) AS kritik_sayi,
            COUNT(*) FILTER (WHERE iade_pct >= 30 AND iade_pct < 50 AND brut_ciro > 50000) AS yuksek_sayi,
            COUNT(*) FILTER (WHERE brut_adet >= 20 AND iade_pct < 15) AS saglikli_sayi,
            SUM(iade_ciro) FILTER (WHERE iade_pct >= 50 AND brut_ciro > 50000) AS kritik_iade_ciro,
            SUM(iade_ciro) FILTER (WHERE iade_pct >= 30 AND brut_ciro > 50000) AS yuksek_iade_ciro
        FROM combined
        """
    ), params)).mappings().first()

    return {
        "kritik": kritik,
        "yuksek": yuksek,
        "saglikli": saglikli,
        "stats": {
            "kritik_sayi": int(stats["kritik_sayi"] or 0),
            "yuksek_sayi": int(stats["yuksek_sayi"] or 0),
            "saglikli_sayi": int(stats["saglikli_sayi"] or 0),
            "kritik_iade_ciro": float(stats["kritik_iade_ciro"] or 0),
            "yuksek_iade_ciro": float(stats["yuksek_iade_ciro"] or 0),
        }
    }


# ── PLM Katalog ───────────────────────────────────────────────────────────────

async def get_plm_katalog(
    session: AsyncSession,
    marka: Optional[str] = None,
    sezon: Optional[str] = None,
    tema: Optional[str] = None,
) -> dict:
    """PLM ürün kataloğu analizi — pim_products tablosundan, e-ticaret filtresi yok."""
    base_conds: List[str] = []
    params: Dict[str, Any] = {}
    if marka:
        base_conds.append("marka_adi = :marka")
        params["marka"] = marka
    if sezon:
        base_conds.append("sezon_adi = :sezon")
        params["sezon"] = sezon
    if tema:
        base_conds.append("tema_adi = :tema")
        params["tema"] = tema

    def _w(*extra: str) -> str:
        conds = base_conds + list(extra)
        return ("WHERE " + " AND ".join(conds)) if conds else ""

    # KPIs
    kpi = (await session.execute(text(f"""
        SELECT
            COUNT(*)                                                              AS toplam,
            COUNT(*) FILTER (WHERE internet_aktif = true)                         AS aktif,
            COUNT(*) FILTER (WHERE bloke = true)                                  AS bloke,
            COUNT(*) FILTER (WHERE (internet_aktif IS NULL OR NOT internet_aktif)
                              AND (bloke IS NULL OR NOT bloke))                   AS pasif,
            COUNT(DISTINCT NULLIF(TRIM(marka_adi),''))                            AS marka_sayisi,
            COUNT(DISTINCT NULLIF(TRIM(sezon_adi),''))                            AS sezon_sayisi,
            COUNT(DISTINCT NULLIF(TRIM(tema_adi),''))                             AS tema_sayisi,
            COUNT(DISTINCT NULLIF(TRIM(ana_grup_adi),''))                         AS ana_grup_sayisi,
            COUNT(DISTINCT NULLIF(TRIM(urun_grubu_adi),''))                       AS urun_grubu_sayisi
        FROM pim_products {_w()}
    """), params)).mappings().one()

    # Marka bazında
    marka_rows = (await session.execute(text(f"""
        SELECT COALESCE(NULLIF(TRIM(marka_adi),''), 'Diğer') AS marka,
               COUNT(*)                             AS urun_sayisi,
               COUNT(*) FILTER (WHERE internet_aktif) AS aktif,
               COUNT(*) FILTER (WHERE bloke)          AS bloke
        FROM pim_products {_w()}
        GROUP BY marka_adi ORDER BY urun_sayisi DESC
    """), params)).mappings().all()

    # Son 8 sezon bazında
    sezon_rows = (await session.execute(text(f"""
        SELECT sezon_adi, sezon_kodu,
               COUNT(*)                              AS urun_sayisi,
               COUNT(*) FILTER (WHERE internet_aktif) AS aktif,
               COUNT(DISTINCT NULLIF(TRIM(tema_adi),'')) AS tema_sayisi
        FROM pim_products {_w('sezon_adi IS NOT NULL')}
        GROUP BY sezon_adi, sezon_kodu
        ORDER BY sezon_kodu DESC NULLS LAST LIMIT 8
    """), params)).mappings().all()

    # Tema bazında
    tema_rows = (await session.execute(text(f"""
        SELECT tema_adi,
               COUNT(*)                                  AS urun_sayisi,
               COUNT(*) FILTER (WHERE internet_aktif)     AS aktif,
               COUNT(DISTINCT NULLIF(TRIM(marka_adi),'')) AS marka_sayisi
        FROM pim_products {_w('tema_adi IS NOT NULL')}
        GROUP BY tema_adi ORDER BY urun_sayisi DESC LIMIT 20
    """), params)).mappings().all()

    # Ana grup + ürün grubu breakdown
    ana_grup_rows = (await session.execute(text(f"""
        SELECT COALESCE(NULLIF(TRIM(ana_grup_adi),''), 'Diğer')   AS ana_grup,
               COALESCE(NULLIF(TRIM(urun_grubu_adi),''), 'Diğer') AS urun_grubu,
               COUNT(*)                              AS urun_sayisi,
               COUNT(*) FILTER (WHERE internet_aktif) AS aktif
        FROM pim_products {_w()}
        GROUP BY ana_grup_adi, urun_grubu_adi ORDER BY urun_sayisi DESC LIMIT 40
    """), params)).mappings().all()

    # Filter options — her zaman filtresiz
    marka_opts = (await session.execute(text(
        "SELECT DISTINCT TRIM(marka_adi) FROM pim_products "
        "WHERE marka_adi IS NOT NULL AND TRIM(marka_adi)!='' ORDER BY 1"
    ))).scalars().all()
    sezon_opts = (await session.execute(text(
        "SELECT sezon_adi FROM ("
        "  SELECT DISTINCT sezon_adi, sezon_kodu FROM pim_products WHERE sezon_adi IS NOT NULL"
        ") s ORDER BY sezon_kodu DESC NULLS LAST LIMIT 20"
    ))).scalars().all()
    tema_opts = (await session.execute(text(
        "SELECT DISTINCT TRIM(tema_adi) FROM pim_products "
        "WHERE tema_adi IS NOT NULL AND TRIM(tema_adi)!='' ORDER BY 1"
    ))).scalars().all()

    return {
        "kpis": {
            "toplam":          int(kpi["toplam"]),
            "aktif":           int(kpi["aktif"]),
            "bloke":           int(kpi["bloke"]),
            "pasif":           int(kpi["pasif"]),
            "marka_sayisi":    int(kpi["marka_sayisi"]),
            "sezon_sayisi":    int(kpi["sezon_sayisi"]),
            "tema_sayisi":     int(kpi["tema_sayisi"]),
            "ana_grup_sayisi": int(kpi["ana_grup_sayisi"]),
            "urun_grubu_sayisi": int(kpi["urun_grubu_sayisi"]),
        },
        "marka": [{"marka": r["marka"], "urun_sayisi": int(r["urun_sayisi"]),
                   "aktif": int(r["aktif"]), "bloke": int(r["bloke"])}
                  for r in marka_rows],
        "sezon": [{"sezon_adi": r["sezon_adi"], "sezon_kodu": r["sezon_kodu"],
                   "urun_sayisi": int(r["urun_sayisi"]), "aktif": int(r["aktif"]),
                   "tema_sayisi": int(r["tema_sayisi"])}
                  for r in sezon_rows],
        "tema": [{"tema_adi": r["tema_adi"], "urun_sayisi": int(r["urun_sayisi"]),
                  "aktif": int(r["aktif"]), "marka_sayisi": int(r["marka_sayisi"])}
                 for r in tema_rows],
        "ana_grup": [{"ana_grup": r["ana_grup"], "urun_grubu": r["urun_grubu"],
                      "urun_sayisi": int(r["urun_sayisi"]), "aktif": int(r["aktif"])}
                     for r in ana_grup_rows],
        "filters": {
            "markalar": list(marka_opts),
            "sezonlar": [s for s in sezon_opts if s],
            "temalar":  [t for t in tema_opts if t],
        },
    }


async def get_urun_yonetimi(
    session: AsyncSession,
    marka: Optional[str] = None,
    sezon: Optional[str] = None,
    tema: Optional[str] = None,
) -> dict:
    """Ürün Yönetimi Dashboard — pim_products üzerinden SKU bazlı analiz."""
    base_conds: List[str] = []
    params: Dict[str, Any] = {}
    if marka:
        base_conds.append("marka_adi = :uy_marka")
        params["uy_marka"] = marka
    if sezon:
        base_conds.append("sezon_adi = :uy_sezon")
        params["uy_sezon"] = sezon
    if tema:
        base_conds.append("tema_adi = :uy_tema")
        params["uy_tema"] = tema

    def _w(*extra: str) -> str:
        conds = base_conds + list(extra)
        return ("WHERE " + " AND ".join(conds)) if conds else ""

    # Hero KPIs
    kpi = (await session.execute(text(f"""
        SELECT
            COUNT(*)                                            AS toplam_sku,
            COUNT(*) FILTER (WHERE internet_aktif)              AS aktif_sku,
            COUNT(*) FILTER (WHERE bloke)                       AS bloke_sku,
            COUNT(DISTINCT sezon_kodu)                          AS sezon_sayisi,
            COUNT(DISTINCT NULLIF(TRIM(tema_adi), ''))          AS tema_sayisi,
            COUNT(DISTINCT NULLIF(TRIM(ana_grup_adi), ''))      AS ana_grup_sayisi,
            COUNT(DISTINCT NULLIF(TRIM(urun_grubu_adi), ''))    AS urun_grubu_sayisi,
            COUNT(DISTINCT NULLIF(TRIM(marka_adi), ''))         AS marka_sayisi
        FROM pim_products {_w()}
    """), params)).mappings().one()

    # Drill-down L1: Marka
    marka_rows = (await session.execute(text(f"""
        SELECT marka_adi,
               COUNT(*)                                          AS sku,
               COUNT(*) FILTER (WHERE internet_aktif)            AS aktif_sku,
               COUNT(DISTINCT sezon_kodu)                        AS sezon_sayisi,
               COUNT(DISTINCT NULLIF(TRIM(tema_adi), ''))        AS tema_sayisi,
               ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pay_pct
        FROM pim_products {_w("marka_adi IS NOT NULL")}
        GROUP BY marka_adi ORDER BY sku DESC
    """), params)).mappings().all()

    # Drill-down L2: Sezon (marka bazında pay)
    sezon_rows = (await session.execute(text(f"""
        SELECT marka_adi, sezon_kodu, sezon_adi,
               COUNT(*)                                           AS sku,
               COUNT(*) FILTER (WHERE internet_aktif)             AS aktif_sku,
               COUNT(DISTINCT NULLIF(TRIM(ana_grup_adi), ''))     AS ana_grup_sayisi,
               ROUND(COUNT(*) * 100.0
                 / NULLIF(SUM(COUNT(*)) OVER (PARTITION BY marka_adi), 0), 1) AS pay_pct
        FROM pim_products {_w("sezon_adi IS NOT NULL")}
        GROUP BY marka_adi, sezon_kodu, sezon_adi
        ORDER BY marka_adi, sezon_kodu DESC NULLS LAST
        LIMIT 40
    """), params)).mappings().all()

    # Drill-down L3: Ana grup (marka+sezon bazında)
    ana_grup_dd = (await session.execute(text(f"""
        SELECT marka_adi, sezon_kodu,
               COALESCE(NULLIF(TRIM(ana_grup_adi), ''), 'Diğer') AS ana_grup,
               COUNT(*)                                            AS sku,
               COUNT(*) FILTER (WHERE internet_aktif)              AS aktif_sku
        FROM pim_products {_w()}
        GROUP BY marka_adi, sezon_kodu, ana_grup_adi
        ORDER BY marka_adi, sezon_kodu DESC NULLS LAST, sku DESC
    """), params)).mappings().all()

    # Sezon kartları (en yeni 5)
    sezon_kartlar = (await session.execute(text(f"""
        SELECT sezon_kodu, sezon_adi,
               COUNT(*)                                           AS sku,
               COUNT(*) FILTER (WHERE internet_aktif)             AS aktif_sku,
               COUNT(DISTINCT NULLIF(TRIM(tema_adi), ''))         AS tema_sayisi,
               COUNT(DISTINCT NULLIF(TRIM(ana_grup_adi), ''))     AS ana_grup_sayisi,
               COUNT(DISTINCT NULLIF(TRIM(marka_adi), ''))        AS marka_sayisi
        FROM pim_products {_w("sezon_adi IS NOT NULL")}
        GROUP BY sezon_kodu, sezon_adi
        ORDER BY sezon_kodu DESC NULLS LAST LIMIT 5
    """), params)).mappings().all()

    # Ana grup tiles (top 7)
    kat_tiles = (await session.execute(text(f"""
        SELECT COALESCE(NULLIF(TRIM(ana_grup_adi), ''), 'Diğer')  AS ana_grup,
               COUNT(*)                                            AS sku,
               COUNT(*) FILTER (WHERE internet_aktif)              AS aktif_sku,
               COUNT(DISTINCT NULLIF(TRIM(tema_adi), ''))          AS tema_sayisi,
               COUNT(DISTINCT NULLIF(TRIM(sezon_kodu), ''))        AS sezon_sayisi,
               ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pay_pct
        FROM pim_products {_w()}
        GROUP BY ana_grup_adi ORDER BY sku DESC LIMIT 7
    """), params)).mappings().all()

    # Tema grid (top 6)
    tema_grid = (await session.execute(text(f"""
        SELECT COALESCE(NULLIF(TRIM(tema_adi), ''), 'Diğer')      AS tema,
               marka_adi,
               COUNT(*)                                            AS sku,
               COUNT(*) FILTER (WHERE internet_aktif)              AS aktif_sku,
               COUNT(DISTINCT sezon_kodu)                          AS sezon_sayisi,
               COUNT(DISTINCT NULLIF(TRIM(ana_grup_adi), ''))      AS ana_grup_sayisi,
               ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pay_pct
        FROM pim_products {_w("tema_adi IS NOT NULL")}
        GROUP BY tema_adi, marka_adi ORDER BY sku DESC LIMIT 6
    """), params)).mappings().all()

    # Isı haritası: Ana Grup × Sezon
    isi_raw = (await session.execute(text(f"""
        SELECT COALESCE(NULLIF(TRIM(ana_grup_adi), ''), 'Diğer') AS ana_grup,
               sezon_kodu, COUNT(*) AS sku
        FROM pim_products {_w("ana_grup_adi IS NOT NULL AND sezon_kodu IS NOT NULL")}
        GROUP BY ana_grup_adi, sezon_kodu
    """), params)).mappings().all()

    top_sezonlar = (await session.execute(text(
        "SELECT sezon_kodu FROM (SELECT DISTINCT sezon_kodu FROM pim_products "
        "WHERE sezon_kodu IS NOT NULL) s ORDER BY sezon_kodu DESC NULLS LAST LIMIT 5"
    ))).scalars().all()

    top_ana_gruplar = (await session.execute(text(f"""
        SELECT COALESCE(NULLIF(TRIM(ana_grup_adi), ''), 'Diğer') AS ana_grup
        FROM pim_products {_w("ana_grup_adi IS NOT NULL")}
        GROUP BY ana_grup ORDER BY COUNT(*) DESC LIMIT 7
    """), params)).scalars().all()

    isi_dict: Dict[str, Any] = {}
    for r in isi_raw:
        ag = r["ana_grup"]
        sz = r["sezon_kodu"]
        if ag not in isi_dict:
            isi_dict[ag] = {}
        isi_dict[ag][sz] = int(r["sku"])

    # Filter options (filtresiz)
    marka_opts = (await session.execute(text(
        "SELECT DISTINCT TRIM(marka_adi) FROM pim_products "
        "WHERE marka_adi IS NOT NULL AND TRIM(marka_adi)!='' ORDER BY 1"
    ))).scalars().all()
    sezon_opts = (await session.execute(text(
        "SELECT sezon_adi FROM (SELECT DISTINCT sezon_adi, sezon_kodu "
        "FROM pim_products WHERE sezon_adi IS NOT NULL) s "
        "ORDER BY sezon_kodu DESC NULLS LAST LIMIT 20"
    ))).scalars().all()
    tema_opts = (await session.execute(text(
        "SELECT DISTINCT TRIM(tema_adi) FROM pim_products "
        "WHERE tema_adi IS NOT NULL AND TRIM(tema_adi)!='' ORDER BY 1"
    ))).scalars().all()

    return {
        "kpis": {
            "toplam_sku":       int(kpi["toplam_sku"]),
            "aktif_sku":        int(kpi["aktif_sku"]),
            "bloke_sku":        int(kpi["bloke_sku"]),
            "sezon_sayisi":     int(kpi["sezon_sayisi"]),
            "tema_sayisi":      int(kpi["tema_sayisi"]),
            "ana_grup_sayisi":  int(kpi["ana_grup_sayisi"]),
            "urun_grubu_sayisi": int(kpi["urun_grubu_sayisi"]),
            "marka_sayisi":     int(kpi["marka_sayisi"]),
        },
        "drill_down": {
            "markalar": [
                {"marka_adi": r["marka_adi"], "sku": int(r["sku"]),
                 "aktif_sku": int(r["aktif_sku"]), "sezon_sayisi": int(r["sezon_sayisi"]),
                 "tema_sayisi": int(r["tema_sayisi"]),
                 "pay_pct": float(r["pay_pct"]) if r["pay_pct"] else 0}
                for r in marka_rows
            ],
            "sezonlar": [
                {"marka_adi": r["marka_adi"], "sezon_kodu": r["sezon_kodu"],
                 "sezon_adi": r["sezon_adi"], "sku": int(r["sku"]),
                 "aktif_sku": int(r["aktif_sku"]),
                 "ana_grup_sayisi": int(r["ana_grup_sayisi"]),
                 "pay_pct": float(r["pay_pct"]) if r["pay_pct"] else 0}
                for r in sezon_rows
            ],
            "ana_gruplar": [
                {"marka_adi": r["marka_adi"], "sezon_kodu": r["sezon_kodu"],
                 "ana_grup": r["ana_grup"], "sku": int(r["sku"]),
                 "aktif_sku": int(r["aktif_sku"])}
                for r in ana_grup_dd
            ],
        },
        "sezon_kartlar": [
            {"sezon_kodu": r["sezon_kodu"], "sezon_adi": r["sezon_adi"],
             "sku": int(r["sku"]), "aktif_sku": int(r["aktif_sku"]),
             "tema_sayisi": int(r["tema_sayisi"]),
             "ana_grup_sayisi": int(r["ana_grup_sayisi"]),
             "marka_sayisi": int(r["marka_sayisi"])}
            for r in sezon_kartlar
        ],
        "ana_gruplar": [
            {"ana_grup": r["ana_grup"], "sku": int(r["sku"]),
             "aktif_sku": int(r["aktif_sku"]), "tema_sayisi": int(r["tema_sayisi"]),
             "sezon_sayisi": int(r["sezon_sayisi"]),
             "pay_pct": float(r["pay_pct"]) if r["pay_pct"] else 0}
            for r in kat_tiles
        ],
        "temalar": [
            {"tema": r["tema"], "marka_adi": r["marka_adi"], "sku": int(r["sku"]),
             "aktif_sku": int(r["aktif_sku"]), "sezon_sayisi": int(r["sezon_sayisi"]),
             "ana_grup_sayisi": int(r["ana_grup_sayisi"]),
             "pay_pct": float(r["pay_pct"]) if r["pay_pct"] else 0}
            for r in tema_grid
        ],
        "isi_haritasi": {
            "sezonlar": list(top_sezonlar),
            "ana_gruplar": list(top_ana_gruplar),
            "data": isi_dict,
        },
        "filters": {
            "markalar": list(marka_opts),
            "sezonlar": [s for s in sezon_opts if s],
            "temalar":  [t for t in tema_opts if t],
        },
    }


# ── Ürün Satış Analiz (Hierarchy + Detail) ───────────────────────────────────

async def get_urun_satis_analiz(
    session: AsyncSession,
    yil: Optional[int] = None,
    aylar: List[int] = None,
    kanallar: List[str] = None,
    marka: Optional[str] = None,
    sezon_kodu: Optional[str] = None,
) -> dict:
    """Marka → Sezon → AnaGrup → ÜrünGrubu hiyerarşisinde satış/iade/iptal analizi.

    incorta_satis / incorta_depo_iade / incorta_iptal_siparis tabloları pim_products
    ile LEFT JOIN edilir. Yıl/ay/kanal filtreleri satış tablolarına, marka/sezon_kodu
    filtreleri pim_products'a uygulanır.
    """
    aylar = aylar or []
    kanallar = kanallar or []

    # ── Satış CTE koşulları (:sa_ prefix — collision önlemi) ──────────────────
    s_conds: List[str] = []
    params: Dict[str, Any] = {}

    if yil:
        s_conds.append("s.yil = :sa_yil")
        params["sa_yil"] = yil
    if aylar:
        s_conds.append("s.ay = ANY(:sa_aylar)")
        params["sa_aylar"] = aylar
    if kanallar:
        s_conds.append("s.satis_kanali = ANY(:sa_kanallar)")
        params["sa_kanallar"] = kanallar

    s_where = ("WHERE " + " AND ".join(s_conds)) if s_conds else ""

    # iade ve iptal CTE'leri: tablo alias'ı farklı, koşullar aynı değerler
    d_where = s_where.replace("s.yil", "d.yil").replace("s.ay", "d.ay").replace("s.satis_kanali", "d.satis_kanali") if s_where else ""
    i_where = s_where.replace("s.yil", "i.yil").replace("s.ay", "i.ay").replace("s.satis_kanali", "i.satis_kanali") if s_where else ""

    # ── PLM filtre koşulları (GROUP BY sonrası HAVING'e eklenir) ──────────────
    plm_conds: List[str] = []
    if marka:
        plm_conds.append("p.marka_adi = :sa_marka")
        params["sa_marka"] = marka
    if sezon_kodu:
        plm_conds.append("p.sezon_kodu = :sa_sezon_kodu")
        params["sa_sezon_kodu"] = sezon_kodu

    plm_where_extra = (" AND " + " AND ".join(plm_conds)) if plm_conds else ""

    sql = text(f"""
        WITH satis AS (
            SELECT s.urun_kodu,
                   SUM(s.tutar)      AS brut_ciro,
                   SUM(s.adet::int)  AS satis_adet
            FROM incorta_satis s {s_where}
            GROUP BY s.urun_kodu
        ),
        iade AS (
            SELECT d.urun_kodu,
                   ABS(SUM(d.tutar))      AS iade_ciro,
                   ABS(SUM(d.adet::int))  AS iade_adet
            FROM incorta_depo_iade d {d_where}
            GROUP BY d.urun_kodu
        ),
        iptal AS (
            SELECT i.urun_kodu,
                   ABS(SUM(i.tutar))      AS iptal_ciro,
                   ABS(SUM(i.adet::int))  AS iptal_adet
            FROM incorta_iptal_siparis i {i_where}
            GROUP BY i.urun_kodu
        )
        SELECT
            p.marka_adi,
            p.sezon_kodu,
            p.sezon_adi,
            COALESCE(NULLIF(TRIM(p.ana_grup_adi),  ''), 'Diğer') AS ana_grup,
            COALESCE(NULLIF(TRIM(p.urun_grubu_adi),''), 'Diğer') AS urun_grubu,
            COUNT(DISTINCT s.urun_kodu)                          AS sku_sayisi,
            COALESCE(SUM(s.satis_adet),  0)                      AS satis_adet,
            COALESCE(SUM(s.brut_ciro),   0)                      AS brut_ciro,
            COALESCE(SUM(ia.iade_adet),  0)                      AS iade_adet,
            COALESCE(SUM(ia.iade_ciro),  0)                      AS iade_ciro,
            COALESCE(SUM(ip.iptal_adet), 0)                      AS iptal_adet,
            COALESCE(SUM(ip.iptal_ciro), 0)                      AS iptal_ciro,
            COALESCE(SUM(s.brut_ciro),   0)
                - COALESCE(SUM(ia.iade_ciro),  0)
                - COALESCE(SUM(ip.iptal_ciro), 0)                AS net_ciro,
            ROUND(
                ((COALESCE(SUM(ia.iade_ciro),0) + COALESCE(SUM(ip.iptal_ciro),0))
                / NULLIF(SUM(s.brut_ciro), 0) * 100)::numeric, 2
            )                                                     AS deger_kaybi_pct,
            ROUND(
                (SUM(s.brut_ciro) / NULLIF(SUM(s.satis_adet), 0))::numeric, 2
            )                                                     AS ort_fiyat
        FROM satis s
        LEFT JOIN iade  ia ON s.urun_kodu = ia.urun_kodu
        LEFT JOIN iptal ip ON s.urun_kodu = ip.urun_kodu
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        WHERE 1=1 {plm_where_extra}
        GROUP BY p.marka_adi, p.sezon_kodu, p.sezon_adi, p.ana_grup_adi, p.urun_grubu_adi
        ORDER BY p.marka_adi NULLS LAST, p.sezon_kodu DESC NULLS LAST,
                 ana_grup, urun_grubu
        LIMIT 300
    """)

    rows = (await session.execute(sql, params)).mappings().all()

    hierarchy = []
    marka_set: set = set()
    sezon_set: set = set()

    for r in rows:
        marka_adi   = r["marka_adi"]
        sezon_k     = r["sezon_kodu"]
        sezon_a     = r["sezon_adi"]
        if marka_adi:
            marka_set.add(marka_adi)
        if sezon_k:
            sezon_set.add(sezon_k)

        brut_ciro       = float(r["brut_ciro"]       or 0)
        iade_ciro       = float(r["iade_ciro"]        or 0)
        iptal_ciro      = float(r["iptal_ciro"]       or 0)
        net_ciro        = float(r["net_ciro"]         or 0)
        deger_kaybi_pct = float(r["deger_kaybi_pct"]  or 0)
        ort_fiyat       = float(r["ort_fiyat"]        or 0)

        hierarchy.append({
            "marka_adi":        marka_adi,
            "sezon_kodu":       sezon_k,
            "sezon_adi":        sezon_a,
            "ana_grup":         r["ana_grup"],
            "urun_grubu":       r["urun_grubu"],
            "sku_sayisi":       int(r["sku_sayisi"]   or 0),
            "satis_adet":       float(r["satis_adet"] or 0),
            "brut_ciro":        brut_ciro,
            "iade_adet":        float(r["iade_adet"]  or 0),
            "iade_ciro":        iade_ciro,
            "iptal_adet":       float(r["iptal_adet"] or 0),
            "iptal_ciro":       iptal_ciro,
            "net_ciro":         net_ciro,
            "deger_kaybi_pct":  deger_kaybi_pct,
            "ort_fiyat":        ort_fiyat,
        })

    return {
        "hierarchy": hierarchy,
        "filters": {
            "markalar": sorted(m for m in marka_set if m),
            "sezonlar": sorted((s for s in sezon_set if s), reverse=True),
        },
    }


async def get_urun_satis_detail(
    session: AsyncSession,
    marka: str,
    sezon_kodu: str,
    ana_grup: str,
    urun_grubu: str,
    yil: Optional[int] = None,
    aylar: List[int] = None,
    kanallar: List[str] = None,
) -> list:
    """Belirli bir marka+sezon_kodu+ana_grup+urun_grubu kombinasyonu için bireysel ürün listesi.

    Aynı CTE pattern'i kullanır; WHERE koşuluna pim_products hiyerarşi filtreleri eklenir.
    """
    aylar = aylar or []
    kanallar = kanallar or []

    # ── Satış CTE koşulları (:sa_ prefix) ────────────────────────────────────
    s_conds: List[str] = []
    params: Dict[str, Any] = {}

    if yil:
        s_conds.append("s.yil = :sa_yil")
        params["sa_yil"] = yil
    if aylar:
        s_conds.append("s.ay = ANY(:sa_aylar)")
        params["sa_aylar"] = aylar
    if kanallar:
        s_conds.append("s.satis_kanali = ANY(:sa_kanallar)")
        params["sa_kanallar"] = kanallar

    s_where = ("WHERE " + " AND ".join(s_conds)) if s_conds else ""
    d_where = s_where.replace("s.yil", "d.yil").replace("s.ay", "d.ay").replace("s.satis_kanali", "d.satis_kanali") if s_where else ""
    i_where = s_where.replace("s.yil", "i.yil").replace("s.ay", "i.ay").replace("s.satis_kanali", "i.satis_kanali") if s_where else ""

    # ── Hiyerarşi WHERE koşulları ─────────────────────────────────────────────
    params["sd_marka"]      = marka
    params["sd_sezon_kodu"] = sezon_kodu
    params["sd_ana_grup"]   = ana_grup
    params["sd_urun_grubu"] = urun_grubu

    sql = text(f"""
        WITH satis AS (
            SELECT s.urun_kodu,
                   SUM(s.tutar)      AS brut_ciro,
                   SUM(s.adet::int)  AS satis_adet
            FROM incorta_satis s {s_where}
            GROUP BY s.urun_kodu
        ),
        iade AS (
            SELECT d.urun_kodu,
                   ABS(SUM(d.tutar))      AS iade_ciro,
                   ABS(SUM(d.adet::int))  AS iade_adet
            FROM incorta_depo_iade d {d_where}
            GROUP BY d.urun_kodu
        ),
        iptal AS (
            SELECT i.urun_kodu,
                   ABS(SUM(i.tutar))      AS iptal_ciro,
                   ABS(SUM(i.adet::int))  AS iptal_adet
            FROM incorta_iptal_siparis i {i_where}
            GROUP BY i.urun_kodu
        )
        SELECT
            p.urun_kodu,
            p.urun_adi,
            p.marka_adi,
            p.internet_aktif,
            p.bloke,
            p.default_image_url,
            p.color_codes,
            COALESCE(s.satis_adet,  0)   AS satis_adet,
            COALESCE(s.brut_ciro,   0)   AS brut_ciro,
            COALESCE(ia.iade_adet,  0)   AS iade_adet,
            COALESCE(ia.iade_ciro,  0)   AS iade_ciro,
            COALESCE(ip.iptal_adet, 0)   AS iptal_adet,
            COALESCE(ip.iptal_ciro, 0)   AS iptal_ciro,
            COALESCE(s.brut_ciro,   0)
                - COALESCE(ia.iade_ciro,  0)
                - COALESCE(ip.iptal_ciro, 0) AS net_ciro,
            ROUND(
                ((COALESCE(ia.iade_ciro,0) + COALESCE(ip.iptal_ciro,0))
                / NULLIF(s.brut_ciro, 0) * 100)::numeric, 2
            )                            AS deger_kaybi_pct,
            ROUND((s.brut_ciro / NULLIF(s.satis_adet, 0))::numeric, 2) AS ort_fiyat
        FROM pim_products p
        LEFT JOIN satis  s  ON s.urun_kodu  = p.urun_kodu
        LEFT JOIN iade   ia ON ia.urun_kodu = p.urun_kodu
        LEFT JOIN iptal  ip ON ip.urun_kodu = p.urun_kodu
        WHERE
            COALESCE(NULLIF(TRIM(p.ana_grup_adi),  ''), 'Diğer') = :sd_ana_grup
            AND COALESCE(NULLIF(TRIM(p.urun_grubu_adi),''), 'Diğer') = :sd_urun_grubu
            AND p.marka_adi   = :sd_marka
            AND p.sezon_kodu  = :sd_sezon_kodu
        ORDER BY net_ciro DESC NULLS LAST
        LIMIT 100
    """)

    rows = (await session.execute(sql, params)).mappings().all()

    result = []
    for r in rows:
        brut_ciro       = float(r["brut_ciro"]       or 0)
        iade_ciro       = float(r["iade_ciro"]        or 0)
        iptal_ciro      = float(r["iptal_ciro"]       or 0)
        net_ciro        = float(r["net_ciro"]         or 0)
        deger_kaybi_pct = float(r["deger_kaybi_pct"]  or 0)
        ort_fiyat       = float(r["ort_fiyat"]        or 0)

        result.append({
            "urun_kodu":        r["urun_kodu"],
            "urun_adi":         r["urun_adi"],
            "marka_adi":        r["marka_adi"],
            "internet_aktif":   bool(r["internet_aktif"]) if r["internet_aktif"] is not None else False,
            "bloke":            bool(r["bloke"])           if r["bloke"]           is not None else False,
            "default_image_url": r["default_image_url"],
            "color_codes":      r["color_codes"] or "",
            "satis_adet":       float(r["satis_adet"]  or 0),
            "brut_ciro":        brut_ciro,
            "iade_adet":        float(r["iade_adet"]   or 0),
            "iade_ciro":        iade_ciro,
            "iptal_adet":       float(r["iptal_adet"]  or 0),
            "iptal_ciro":       iptal_ciro,
            "net_ciro":         net_ciro,
            "deger_kaybi_pct":  deger_kaybi_pct,
            "ort_fiyat":        ort_fiyat,
        })


# ── Günlük E-Ticaret Analizi (incorta_ecommerce_gunluk) ──────────────────────

async def get_eticaret_gunluk(session: AsyncSession, gun_sayisi: int = 30) -> dict:
    """Son N günlük e-ticaret satışları: trend + kanal + top ürünler + KPI özeti."""

    max_gun = await session.execute(text(
        "SELECT MAX(tarih::date) FROM incorta_ecommerce_gunluk"
    ))
    son_gun = max_gun.scalar()
    bas_tarih   = (son_gun - timedelta(days=gun_sayisi - 1)) if son_gun else date.today()
    bas_str     = str(bas_tarih)
    son_str     = str(son_gun) if son_gun else date.today().isoformat()
    dun_str     = str((son_gun - timedelta(days=1)) if son_gun else (date.today() - timedelta(days=1)))

    trend_rows = await session.execute(text("""
        SELECT
            tarih::date                                          AS gun,
            SUM(satis_tutar)                                     AS satis,
            ABS(SUM(COALESCE(iade_tutar, 0)))                    AS iade,
            ABS(SUM(COALESCE(iptal_tutar, 0)))                   AS iptal,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutar, 0))
              + SUM(COALESCE(iptal_tutar, 0))                    AS net,
            SUM(satis_adet)                                      AS brut_adet
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas
        GROUP BY tarih::date
        ORDER BY tarih::date
    """), {"bas": bas_str})
    trend = [dict(r) for r in trend_rows.mappings()]

    # Son günün kanal özeti (MAX tarih)
    son_gun_next = str((son_gun + timedelta(days=1)) if son_gun else (date.today() + timedelta(days=1)))
    kanal_rows = await session.execute(text("""
        SELECT
            satis_kanali,
            SUM(satis_tutar)                                       AS satis,
            ABS(SUM(COALESCE(iade_tutar, 0)))                      AS iade,
            ABS(SUM(COALESCE(iptal_tutar, 0)))                     AS iptal,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutar, 0))
              + SUM(COALESCE(iptal_tutar, 0))                      AS net,
            SUM(satis_adet)                                        AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar, 0)))
              / NULLIF(SUM(satis_tutar), 0) * 100)::numeric, 1)    AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :gun AND tarih < :gun_next
        GROUP BY satis_kanali
        ORDER BY SUM(satis_tutar) DESC
    """), {"gun": son_str, "gun_next": son_gun_next})
    kanal = [dict(r) for r in kanal_rows.mappings()]

    top_rows = await session.execute(text("""
        SELECT
            urun_kodu,
            MAX(urun_adi)                                          AS urun_adi,
            SUM(satis_tutar)                                       AS satis,
            ABS(SUM(COALESCE(iade_tutar, 0)))                      AS iade,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutar, 0))
              + SUM(COALESCE(iptal_tutar, 0))                      AS net,
            SUM(satis_adet)                                        AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar, 0)))
              / NULLIF(SUM(satis_tutar), 0) * 100)::numeric, 1)    AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas
        GROUP BY urun_kodu
        ORDER BY net DESC
        LIMIT 20
    """), {"bas": bas_str})
    top_urunler = [dict(r) for r in top_rows.mappings()]

    # Bugün / dün / dönem KPI (text range comparisons — index-friendly)
    kpi_rows = await session.execute(text("""
        SELECT
            CASE
                WHEN tarih >= :bugun AND tarih < :bugun_next THEN 'bugun'
                WHEN tarih >= :dun   AND tarih < :bugun     THEN 'dun'
                ELSE 'hafta'
            END                                                    AS donem,
            SUM(satis_tutar)                                       AS satis,
            ABS(SUM(COALESCE(iade_tutar, 0)))                      AS iade,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutar, 0))
              + SUM(COALESCE(iptal_tutar, 0))                      AS net,
            SUM(satis_adet)                                        AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar, 0)))
              / NULLIF(SUM(satis_tutar), 0) * 100)::numeric, 1)   AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :hafta_bas
        GROUP BY donem
    """), {
        "bugun":      son_str,
        "bugun_next": son_gun_next,
        "dun":        dun_str,
        "hafta_bas":  bas_str,
    })
    kpis: dict = {"bugun": {}, "dun": {}, "hafta": {}}
    for r in kpi_rows.mappings():
        d = r["donem"]
        if d:
            kpis[d] = {k: v for k, v in r.items() if k != "donem"}

    return {
        "son_gun":    str(son_gun) if son_gun else None,
        "trend":      trend,
        "kanal":      kanal,
        "top_urunler": top_urunler,
        "kpis":       kpis,
    }


# ── Günlük Mağaza Analizi (incorta_magaza_gunluk) ────────────────────────────

async def get_magaza_gunluk(session: AsyncSession, gun_sayisi: int = 30) -> dict:
    """Son N günlük mağaza satışları: trend + top mağazalar + top ürünler + KPI özeti."""
    try:
        await session.execute(text("SELECT 1 FROM incorta_magaza_gunluk LIMIT 1"))
    except Exception:
        await session.rollback()
        return {"hata": "magaza_veri_yok", "trend": [], "kpis": {}, "top_magazalar": [], "top_urunler": []}

    max_gun = await session.execute(text(
        "SELECT MAX(tarih::date) FROM incorta_magaza_gunluk"
    ))
    son_gun = max_gun.scalar()
    bas_tarih   = (son_gun - timedelta(days=gun_sayisi - 1)) if son_gun else date.today()
    bas_str     = str(bas_tarih)
    son_str     = str(son_gun) if son_gun else date.today().isoformat()
    dun_str     = str((son_gun - timedelta(days=1)) if son_gun else (date.today() - timedelta(days=1)))
    son_next    = str((son_gun + timedelta(days=1)) if son_gun else (date.today() + timedelta(days=1)))

    trend_rows = await session.execute(text("""
        SELECT
            tarih::date                                          AS gun,
            SUM(satis_tutar)                                     AS satis,
            ABS(SUM(COALESCE(iade_tutari, 0)))                   AS iade,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutari, 0))    AS net,
            SUM(satis_adet)                                      AS brut_adet
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas
        GROUP BY tarih::date
        ORDER BY tarih::date
    """), {"bas": bas_str})
    trend = [dict(r) for r in trend_rows.mappings()]

    top_mag_rows = await session.execute(text("""
        SELECT
            magaza,
            SUM(satis_tutar)                                         AS satis,
            ABS(SUM(COALESCE(iade_tutari, 0)))                       AS iade,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutari, 0))        AS net,
            SUM(satis_adet)                                          AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutari, 0)))
              / NULLIF(SUM(satis_tutar), 0) * 100)::numeric, 1)      AS iade_pct
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas AND magaza IS NOT NULL AND magaza <> ''
        GROUP BY magaza
        ORDER BY net DESC
        LIMIT 20
    """), {"bas": bas_str})
    top_magazalar = [dict(r) for r in top_mag_rows.mappings()]

    top_urun_rows = await session.execute(text("""
        SELECT
            urun_kodu,
            MAX(urun_adi)                                            AS urun_adi,
            SUM(satis_tutar)                                         AS satis,
            ABS(SUM(COALESCE(iade_tutari, 0)))                       AS iade,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutari, 0))        AS net,
            SUM(satis_adet)                                          AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutari, 0)))
              / NULLIF(SUM(satis_tutar), 0) * 100)::numeric, 1)      AS iade_pct
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas
        GROUP BY urun_kodu
        ORDER BY net DESC
        LIMIT 20
    """), {"bas": bas_str})
    top_urunler = [dict(r) for r in top_urun_rows.mappings()]

    # KPI: text range comparisons — index-friendly
    kpi_rows = await session.execute(text("""
        SELECT
            CASE
                WHEN tarih >= :son AND tarih < :son_next THEN 'bugun'
                WHEN tarih >= :dun AND tarih < :son      THEN 'dun'
                ELSE 'hafta'
            END                                                      AS donem,
            SUM(satis_tutar)                                         AS satis,
            ABS(SUM(COALESCE(iade_tutari, 0)))                       AS iade,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutari, 0))        AS net,
            SUM(satis_adet)                                          AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutari, 0)))
              / NULLIF(SUM(satis_tutar), 0) * 100)::numeric, 1)     AS iade_pct
        FROM incorta_magaza_gunluk
        WHERE tarih >= :hafta_bas
        GROUP BY donem
    """), {
        "son":      son_str,
        "son_next": son_next,
        "dun":      dun_str,
        "hafta_bas": bas_str,
    })
    kpis: dict = {"bugun": {}, "dun": {}, "hafta": {}}
    for r in kpi_rows.mappings():
        d = r["donem"]
        if d:
            kpis[d] = {k: v for k, v in r.items() if k != "donem"}

    return {
        "son_gun":      str(son_gun) if son_gun else None,
        "trend":        trend,
        "top_magazalar": top_magazalar,
        "top_urunler":  top_urunler,
        "kpis":         kpis,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# GÜNLÜK SATIŞ ANALİZ — 15 Günlük Detay
# ═══════════════════════════════════════════════════════════════════════════════

_GUN_ADI  = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]
_GUN_KISA = ["Pzt","Sal","Çar","Per","Cum","Cmt","Paz"]


async def get_gunluk_satis_analiz(session: AsyncSession, gun_sayisi: int = 15) -> dict:
    """Son N günlük mağaza satış detay analizi — gün bazında kırılım + karşılaştırma."""
    try:
        await session.execute(text("SELECT 1 FROM incorta_magaza_gunluk LIMIT 1"))
    except Exception:
        await session.rollback()
        return {"hata": "veri_yok", "gunler": [], "ozet": {}, "son_gun": None}

    max_row = await session.execute(text("SELECT MAX(tarih::date) FROM incorta_magaza_gunluk"))
    son_gun = max_row.scalar()
    if not son_gun:
        return {"hata": "veri_yok", "gunler": [], "ozet": {}, "son_gun": None}

    bas_tarih     = son_gun - timedelta(days=gun_sayisi - 1)
    kar_bas       = bas_tarih - timedelta(days=7)   # 7 gün öncesi karşılaştırma için
    bas_str       = str(bas_tarih)
    kar_bas_str   = str(kar_bas)

    # ── Günlük toplamlar (ana 15 gün + 7 gün öncesi) ─────────────────────────
    rows = await session.execute(text("""
        SELECT
            tarih::date                                           AS gun,
            SUM(satis_tutar)                                      AS brut_satis,
            ABS(SUM(COALESCE(iade_tutari, 0)))                    AS iade,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutari, 0))     AS net_ciro,
            SUM(satis_adet)                                       AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutari, 0)))
              / NULLIF(SUM(satis_tutar), 0) * 100)::numeric, 1)  AS iade_pct,
            COUNT(DISTINCT CASE WHEN magaza IS NOT NULL AND magaza <> '' THEN magaza END)
                                                                  AS aktif_magaza,
            ROUND((SUM(satis_tutar) + SUM(COALESCE(iade_tutari, 0)))
              / NULLIF(SUM(satis_adet), 0))::numeric              AS obf
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas
        GROUP BY tarih::date
        ORDER BY tarih::date
    """), {"bas": kar_bas_str})

    gunluk_map: dict = {}
    for r in rows.mappings():
        gunluk_map[str(r["gun"])] = {
            "brut_satis":   float(r["brut_satis"]  or 0),
            "iade":         float(r["iade"]        or 0),
            "net_ciro":     float(r["net_ciro"]    or 0),
            "adet":         int(r["adet"]          or 0),
            "iade_pct":     float(r["iade_pct"]    or 0),
            "aktif_magaza": int(r["aktif_magaza"]  or 0),
            "obf":          round(float(r["obf"]   or 0)),
        }

    # ── Top 3 mağaza her gün için ─────────────────────────────────────────────
    mag_rows = await session.execute(text("""
        SELECT
            tarih::date                                           AS gun,
            magaza,
            SUM(satis_tutar) + SUM(COALESCE(iade_tutari, 0))     AS net
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas AND magaza IS NOT NULL AND magaza <> ''
        GROUP BY tarih::date, magaza
        ORDER BY tarih::date, net DESC
    """), {"bas": bas_str})

    mag_by_day: dict = {}
    for r in mag_rows.mappings():
        g = str(r["gun"])
        if g not in mag_by_day:
            mag_by_day[g] = []
        if len(mag_by_day[g]) < 3:
            mag_by_day[g].append({"magaza": r["magaza"], "net": round(float(r["net"] or 0))})

    # ── Gün bazlı liste ───────────────────────────────────────────────────────
    gunler = []
    cur = bas_tarih
    while cur <= son_gun:
        ts = str(cur)
        dow = cur.weekday()
        d = gunluk_map.get(ts, {})
        net = d.get("net_ciro", 0)

        prev1_net  = gunluk_map.get(str(cur - timedelta(days=1)), {}).get("net_ciro", 0)
        prev7_net  = gunluk_map.get(str(cur - timedelta(days=7)), {}).get("net_ciro", 0)
        g_degisim  = round((net - prev1_net) / prev1_net * 100, 1) if prev1_net else None
        hf_degisim = round((net - prev7_net) / prev7_net * 100, 1) if prev7_net else None

        # Türkçe tarih etiketi
        ay_kisaltma = ["Oca","Şub","Mar","Nis","May","Haz","Tem","Ağu","Eyl","Eki","Kas","Ara"]
        tarih_label = f"{cur.day} {ay_kisaltma[cur.month-1]}"

        gunler.append({
            "tarih":            ts,
            "tarih_label":      tarih_label,
            "gun_adi":          _GUN_ADI[dow],
            "gun_kisa":         _GUN_KISA[dow],
            "haftasonu":        dow >= 5,
            "brut_satis":       round(d.get("brut_satis", 0)),
            "iade":             round(d.get("iade", 0)),
            "net_ciro":         round(net),
            "adet":             d.get("adet", 0),
            "iade_pct":         d.get("iade_pct", 0),
            "aktif_magaza":     d.get("aktif_magaza", 0),
            "onceki_gun_net":   round(prev1_net) if prev1_net else None,
            "gun_degisim_pct":  g_degisim,
            "gecen_hafta_net":  round(prev7_net) if prev7_net else None,
            "hf_degisim_pct":   hf_degisim,
            "obf":              d.get("obf", 0),
            "top_magazalar":    mag_by_day.get(ts, []),
            "veri_var":         net > 0,
        })
        cur += timedelta(days=1)

    # ── Özet istatistikler ────────────────────────────────────────────────────
    veri = [g for g in gunler if g["net_ciro"] > 0]
    if veri:
        nets         = [g["net_ciro"] for g in veri]
        total_adet_v = sum(g["adet"] for g in veri)
        ort          = sum(nets) / len(nets)
        en_iyi       = max(veri, key=lambda g: g["net_ciro"])
        en_kotu      = min(veri, key=lambda g: g["net_ciro"])
        hici         = [g for g in veri if not g["haftasonu"]]
        hson         = [g for g in veri if g["haftasonu"]]
        ozet = {
            "toplam_net":      round(sum(nets)),
            "ortalama_gunluk": round(ort),
            "en_iyi_gun":      {"tarih": en_iyi["tarih"], "tarih_label": en_iyi["tarih_label"],
                                 "gun_adi": en_iyi["gun_adi"], "net": en_iyi["net_ciro"]},
            "en_kotu_gun":     {"tarih": en_kotu["tarih"], "tarih_label": en_kotu["tarih_label"],
                                 "gun_adi": en_kotu["gun_adi"], "net": en_kotu["net_ciro"]},
            "haftaici_ort":    round(sum(g["net_ciro"] for g in hici) / len(hici)) if hici else 0,
            "haftasonu_ort":   round(sum(g["net_ciro"] for g in hson) / len(hson)) if hson else 0,
            "veri_gun_sayisi": len(veri),
            "obf_donem":       round(sum(nets) / total_adet_v) if total_adet_v > 0 else 0,
        }
    else:
        ozet = {}

    # ── Aylık KPI'lar: hedef, ziyaretçi, MDO, sepet, OBF (incorta_magaza_performans) ──
    aylik_kpi: dict = {"hedef": None, "ziyaretci": None, "mdo": None, "sepet": None, "obf_perf": None}
    try:
        pr = await session.execute(text("""
            SELECT SUM(hedef)      AS hedef,
                   SUM(ziyaretci) AS ziyaretci,
                   AVG(mdo)       AS mdo,
                   AVG(sepet)     AS sepet,
                   AVG(obf)       AS obf
            FROM incorta_magaza_performans
            WHERE (bolge_muduru IS NULL OR bolge_muduru = '')
              AND (magaza IS NULL OR magaza = '')
              AND (yil * 100 + ay) BETWEEN :bas_myy AND :son_myy
        """), {
            "bas_myy": int(f"{bas_tarih.year}{bas_tarih.month:02d}"),
            "son_myy": int(f"{son_gun.year}{son_gun.month:02d}"),
        })
        p = pr.mappings().one_or_none()
        if p and p["hedef"] is not None:
            aylik_kpi = {
                "hedef":    round(float(p["hedef"]    or 0)),
                "ziyaretci": round(float(p["ziyaretci"] or 0)),
                "mdo":       round(float(p["mdo"]      or 0), 1),
                "sepet":     round(float(p["sepet"]    or 0)),
                "obf_perf":  round(float(p["obf"]      or 0)),
            }
    except Exception:
        await session.rollback()

    return {
        "son_gun":    str(son_gun),
        "bas_tarih":  str(bas_tarih),
        "gun_sayisi": gun_sayisi,
        "gunler":     gunler,
        "ozet":       ozet,
        "aylik_kpi":  aylik_kpi,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ADL RAPORLAR — 5 Rapor Sistemi
# ═══════════════════════════════════════════════════════════════════════════════

_KOMISYON = {
    "ADL": 0.0, "ADL IOS APP": 0.0, "ADL ANDROID APP": 0.0,
    "LOVEMYBODY": 0.0, "LMB IOS APP": 0.0, "LMB ANDROID APP": 0.0,
    "TY ADL AZ": 0.18, "TY LMB AZ": 0.18,
    "TRENDYOL": 0.18, "HEPSIBURADA": 0.15, "BOYNER": 0.15, "AMAZON": 0.15,
}

def _komisyon_sonrasi(net: float, kanal: str) -> float:
    rate = _KOMISYON.get(kanal, 0.15)
    return net * (1 - rate)

def _min_yyay(ay_count: int) -> int:
    today = date.today()
    m = today.month - ay_count + 1
    y = today.year
    while m <= 0:
        m += 12
        y -= 1
    return y * 100 + m


async def get_adl_yonetici(session: AsyncSession, gun_sayisi: int = 7) -> dict:
    """Yönetici Özeti: e-ticaret + mağaza birleşik günlük özet."""
    # ── E-Ticaret son gün ──────────────────────────────────────────────────
    eg_max = await session.execute(text(
        "SELECT MAX(tarih::date) FROM incorta_ecommerce_gunluk"
    ))
    eg_son = eg_max.scalar()
    if eg_son:
        eg_bas = eg_son - timedelta(days=gun_sayisi - 1)
        eg_bas_str  = str(eg_bas)
        eg_son_str  = str(eg_son)
        eg_dun_str  = str(eg_son - timedelta(days=1))
        eg_next_str = str(eg_son + timedelta(days=1))
    else:
        eg_bas_str = eg_son_str = eg_dun_str = eg_next_str = str(date.today())

    eg_kpi_rows = await session.execute(text("""
        SELECT
            CASE WHEN tarih >= :son AND tarih < :next THEN 'bugun'
                 WHEN tarih >= :dun AND tarih < :son  THEN 'dun'
                 ELSE 'hafta' END AS donem,
            SUM(satis_tutar)                                                                    AS satis,
            ABS(SUM(COALESCE(iade_tutar, 0)))                                                  AS iade,
            ABS(SUM(COALESCE(iptal_tutar, 0)))                                                 AS iptal,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0))        AS net,
            SUM(satis_adet)                                                                    AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas
        GROUP BY donem
    """), {"bas": eg_bas_str, "son": eg_son_str, "next": eg_next_str, "dun": eg_dun_str})
    eg_kpis: dict = {"bugun": {}, "dun": {}, "hafta": {}}
    for r in eg_kpi_rows.mappings():
        d = r["donem"]
        if d:
            eg_kpis[d] = {k: float(v) if v is not None else 0.0 for k, v in r.items() if k != "donem"}

    # ── Kanal breakdown (son gün) ──────────────────────────────────────────
    kanal_rows = await session.execute(text("""
        SELECT satis_kanali,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutar,0))) AS iade,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :son AND tarih < :next
        GROUP BY satis_kanali
        ORDER BY net DESC
        LIMIT 8
    """), {"son": eg_son_str, "next": eg_next_str})
    top_kanallar = [dict(r) for r in kanal_rows.mappings()]
    for k in top_kanallar:
        k["komisyon_net"] = round(_komisyon_sonrasi(float(k.get("net") or 0), k.get("satis_kanali", "")), 2)

    # ── Mağaza son gün ────────────────────────────────────────────────────
    mg_son = None
    mg_kpis: dict = {"bugun": {}, "dun": {}, "hafta": {}}
    mg_bas_str = mg_son_str = mg_dun_str = mg_next_str = str(date.today())
    try:
        mg_max = await session.execute(text(
            "SELECT MAX(tarih::date) FROM incorta_magaza_gunluk"
        ))
        mg_son = mg_max.scalar()
        if mg_son:
            mg_bas = mg_son - timedelta(days=gun_sayisi - 1)
            mg_bas_str  = str(mg_bas)
            mg_son_str  = str(mg_son)
            mg_dun_str  = str(mg_son - timedelta(days=1))
            mg_next_str = str(mg_son + timedelta(days=1))
        else:
            mg_bas_str = mg_son_str = mg_dun_str = mg_next_str = str(date.today())

        mg_kpi_rows = await session.execute(text("""
            SELECT
                CASE WHEN tarih >= :son AND tarih < :next THEN 'bugun'
                     WHEN tarih >= :dun AND tarih < :son  THEN 'dun'
                     ELSE 'hafta' END AS donem,
                SUM(satis_tutar) AS satis,
                ABS(SUM(COALESCE(iade_tutari,0))) AS iade,
                SUM(satis_tutar)+SUM(COALESCE(iade_tutari,0)) AS net,
                SUM(satis_adet) AS adet,
                ROUND((ABS(SUM(COALESCE(iade_tutari,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
            FROM incorta_magaza_gunluk
            WHERE tarih >= :bas AND magaza IS NOT NULL AND magaza <> ''
            GROUP BY donem
        """), {"bas": mg_bas_str, "son": mg_son_str, "next": mg_next_str, "dun": mg_dun_str})
        for r in mg_kpi_rows.mappings():
            d = r["donem"]
            if d:
                mg_kpis[d] = {k: float(v) if v is not None else 0.0 for k, v in r.items() if k != "donem"}
    except Exception:
        await session.rollback()
        mg_son = None
        mg_kpis = {"bugun": {}, "dun": {}, "hafta": {}}

    # ── Risk uyarıları ────────────────────────────────────────────────────
    risk_uyarilari = []
    for k in top_kanallar:
        ip = float(k.get("iade_pct") or 0)
        if ip >= 30:
            risk_uyarilari.append({"tip": "kritik", "mesaj": f"{k['satis_kanali']} — İade oranı %{ip:.1f} (kritik eşik: %30)", "kanal": k["satis_kanali"]})
        elif ip >= 20:
            risk_uyarilari.append({"tip": "uyari", "mesaj": f"{k['satis_kanali']} — İade oranı %{ip:.1f} (uyarı eşiği: %20)", "kanal": k["satis_kanali"]})

    # ── WoW karşılaştırma (önceki aynı periyot) ──────────────────────────
    eg_prev_bas = str(eg_son - timedelta(days=gun_sayisi * 2 - 1)) if eg_son else eg_bas_str
    eg_prev_son = str(eg_son - timedelta(days=gun_sayisi)) if eg_son else eg_bas_str
    mg_prev_bas = str(mg_son - timedelta(days=gun_sayisi * 2 - 1)) if mg_son else mg_bas_str
    mg_prev_son = str(mg_son - timedelta(days=gun_sayisi)) if mg_son else mg_bas_str

    eg_wow_rows = await session.execute(text("""
        SELECT SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
               ABS(SUM(COALESCE(iade_tutar,0))) AS iade
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas AND tarih < :son
    """), {"bas": eg_prev_bas, "son": eg_bas_str})
    eg_prev = dict(eg_wow_rows.mappings().one_or_none() or {})

    try:
        mg_wow_rows = await session.execute(text("""
            SELECT SUM(satis_tutar)+SUM(COALESCE(iade_tutari,0)) AS net,
                   ABS(SUM(COALESCE(iade_tutari,0))) AS iade
            FROM incorta_magaza_gunluk
            WHERE tarih >= :bas AND tarih < :son AND magaza IS NOT NULL AND magaza <> ''
        """), {"bas": mg_prev_bas, "son": mg_bas_str})
        mg_prev = dict(mg_wow_rows.mappings().one_or_none() or {})
    except Exception:
        await session.rollback()
        mg_prev = {}

    def _wow_pct(cur, prv):
        c, p = float(cur or 0), float(prv or 0)
        return round((c - p) / abs(p) * 100, 1) if p else None

    donem_karsilastirma = {
        "eg_net_simdi":   float(eg_kpis.get("hafta", {}).get("net") or 0),
        "eg_net_onceki":  float(eg_prev.get("net") or 0),
        "eg_wow_pct":     _wow_pct(eg_kpis.get("hafta", {}).get("net"), eg_prev.get("net")),
        "eg_iade_simdi":  float(eg_kpis.get("hafta", {}).get("iade") or 0),
        "eg_iade_onceki": float(eg_prev.get("iade") or 0),
        "mg_net_simdi":   float(mg_kpis.get("hafta", {}).get("net") or 0),
        "mg_net_onceki":  float(mg_prev.get("net") or 0),
        "mg_wow_pct":     _wow_pct(mg_kpis.get("hafta", {}).get("net"), mg_prev.get("net")),
        "toplam_simdi":   float(eg_kpis.get("hafta", {}).get("net") or 0) + float(mg_kpis.get("hafta", {}).get("net") or 0),
        "toplam_onceki":  float(eg_prev.get("net") or 0) + float(mg_prev.get("net") or 0),
    }
    donem_karsilastirma["toplam_wow_pct"] = _wow_pct(
        donem_karsilastirma["toplam_simdi"], donem_karsilastirma["toplam_onceki"]
    )

    # ── Top 5 / Bottom 5 SKU (e-ticaret, son periyot) ────────────────────
    top_sku_rows = await session.execute(text("""
        SELECT urun_kodu, MAX(urun_adi) AS urun_adi,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas
        GROUP BY urun_kodu
        ORDER BY net DESC
        LIMIT 5
    """), {"bas": eg_bas_str})
    top5_sku = [dict(r) for r in top_sku_rows.mappings()]

    bot_sku_rows = await session.execute(text("""
        SELECT urun_kodu, MAX(urun_adi) AS urun_adi,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas AND satis_adet > 0
        GROUP BY urun_kodu
        HAVING SUM(satis_tutar) > 0
        ORDER BY net ASC
        LIMIT 5
    """), {"bas": eg_bas_str})
    bottom5_sku = [dict(r) for r in bot_sku_rows.mappings()]

    return {
        "eg_son_gun":           str(eg_son) if eg_son else None,
        "mg_son_gun":           str(mg_son) if mg_son else None,
        "gun_sayisi":           gun_sayisi,
        "eg_kpis":              eg_kpis,
        "mg_kpis":              mg_kpis,
        "top_kanallar":         top_kanallar,
        "risk_uyarilari":       risk_uyarilari,
        "donem_karsilastirma":  donem_karsilastirma,
        "top5_sku":             top5_sku,
        "bottom5_sku":          bottom5_sku,
    }


async def get_adl_eticaret(session: AsyncSession, gun_sayisi: int = 30) -> dict:
    """E-Ticaret Raporu: kanal detayı, komisyon, iade matrisi."""
    max_r = await session.execute(text("SELECT MAX(tarih::date) FROM incorta_ecommerce_gunluk"))
    son_gun = max_r.scalar()
    if son_gun:
        bas_tarih = son_gun - timedelta(days=gun_sayisi - 1)
        bas_str     = str(bas_tarih)
        son_str     = str(son_gun)
        dun_str     = str(son_gun - timedelta(days=1))
        son_next    = str(son_gun + timedelta(days=1))
    else:
        bas_str = son_str = dun_str = son_next = str(date.today())

    # KPI
    kpi_rows = await session.execute(text("""
        SELECT
            CASE WHEN tarih >= :son AND tarih < :next THEN 'bugun'
                 WHEN tarih >= :dun AND tarih < :son  THEN 'dun'
                 ELSE 'donem' END AS donem,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutar,0))) AS iade,
            ABS(SUM(COALESCE(iptal_tutar,0))) AS iptal,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas
        GROUP BY donem
    """), {"bas": bas_str, "son": son_str, "next": son_next, "dun": dun_str})
    kpis: dict = {"bugun": {}, "dun": {}, "donem": {}}
    for r in kpi_rows.mappings():
        d = r["donem"]
        if d:
            kpis[d] = {k: float(v) if v is not None else 0.0 for k, v in r.items() if k != "donem"}

    # Kanal performance (full period)
    kanal_rows = await session.execute(text("""
        SELECT satis_kanali,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutar,0))) AS iade,
            ABS(SUM(COALESCE(iptal_tutar,0))) AS iptal,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct,
            ROUND((SUM(satis_tutar)/NULLIF(SUM(SUM(satis_tutar))OVER(),0)*100)::numeric,1) AS pazar_payi
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas
        GROUP BY satis_kanali
        ORDER BY net DESC
    """), {"bas": bas_str})
    kanal_ozet = []
    for r in kanal_rows.mappings():
        row = dict(r)
        net = float(row.get("net") or 0)
        row["komisyon_net"] = round(_komisyon_sonrasi(net, row.get("satis_kanali", "")), 2)
        row["komisyon_oran"] = round(_KOMISYON.get(row.get("satis_kanali", ""), 0.15) * 100, 0)
        kanal_ozet.append(row)

    # Günlük trend
    trend_rows = await session.execute(text("""
        SELECT tarih::date AS gun,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutar,0))) AS iade,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
            SUM(satis_adet) AS adet
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas
        GROUP BY tarih::date
        ORDER BY tarih::date
    """), {"bas": bas_str})
    trend = [dict(r) for r in trend_rows.mappings()]

    # Top ürünler (son gün)
    top_rows = await session.execute(text("""
        SELECT urun_kodu, MAX(urun_adi) AS urun_adi,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutar,0))) AS iade,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas
        GROUP BY urun_kodu
        ORDER BY net DESC
        LIMIT 20
    """), {"bas": bas_str})
    top_urunler = [dict(r) for r in top_rows.mappings()]

    # İade matrisi: ürün × beden × renk (son ay, monthly data)
    today = date.today()
    cur_yyay = today.year * 100 + today.month
    prev_m = today.month - 1 if today.month > 1 else 12
    prev_y = today.year if today.month > 1 else today.year - 1
    prev_yyay = prev_y * 100 + prev_m
    iade_mat_rows = await session.execute(text("""
        SELECT d.urun_kodu, MAX(d.urun_adi) AS urun_adi,
            MAX(d.renk) AS renk, MAX(d.beden) AS beden,
            ABS(SUM(d.tutar)) AS iade_tutar,
            ABS(SUM(d.adet::int)) AS iade_adet,
            ROUND((ABS(SUM(d.tutar)) / NULLIF(SUM(s.tutar), 0) * 100)::numeric, 1) AS iade_orani
        FROM incorta_depo_iade d
        LEFT JOIN incorta_satis s
            ON d.urun_kodu=s.urun_kodu AND d.yil=s.yil AND d.ay=s.ay
           AND d.satis_kanali=s.satis_kanali AND d.renk=s.renk AND d.beden=s.beden
        WHERE (d.yil*100+d.ay) >= :min_yyay
        GROUP BY d.urun_kodu, d.renk, d.beden
        HAVING ABS(SUM(d.adet::int)) >= 3
        ORDER BY iade_orani DESC NULLS LAST
        LIMIT 10
    """), {"min_yyay": prev_yyay})
    iade_matrisi = [dict(r) for r in iade_mat_rows.mappings()]

    # GA4 özet (incorta_analytics son hafta)
    analytics_from = (date.today() - timedelta(days=gun_sayisi)).isoformat()
    ga4_rows = await session.execute(text("""
        SELECT
            ROUND(AVG(conversion_rate)::numeric * 100, 2) AS conversion_pct,
            ROUND(AVG(hemen_cikma_orani)::numeric * 100, 1) AS bounce_pct,
            SUM(oturumlar) AS toplam_oturum,
            SUM(kullanicilar) AS toplam_kullanici,
            SUM(CASE WHEN oturum_kaynagi ILIKE '%organic%' THEN ciro ELSE 0 END) AS organik_ciro,
            SUM(CASE WHEN oturum_kaynagi ILIKE '%cpc%' OR oturum_kaynagi ILIKE '%paid%' OR oturum_kaynagi ILIKE '%ads%' THEN ciro ELSE 0 END) AS ucretli_ciro
        FROM incorta_analytics
        WHERE date >= :analytics_from
    """), {"analytics_from": analytics_from})
    ga4_row = ga4_rows.mappings().one_or_none()
    ga4_ozet = dict(ga4_row) if ga4_row else {}

    # SKU çeşitliliği + ort. sepet (donem KPI'larına ekle)
    sku_row = await session.execute(text("""
        SELECT COUNT(DISTINCT urun_kodu) AS sku_cesitliligi
        FROM incorta_ecommerce_gunluk WHERE tarih >= :bas
    """), {"bas": bas_str})
    kpis["donem"]["sku_cesitliligi"] = sku_row.scalar() or 0
    _dn = kpis["donem"]
    _dn["ort_sepet"] = round(float(_dn.get("net") or 0) / max(float(_dn.get("adet") or 1), 1), 2)

    # Kanal önceki dönem (WoW)
    onceki_bas = str(bas_tarih - timedelta(days=gun_sayisi)) if son_gun else bas_str
    onceki_rows = await session.execute(text("""
        SELECT satis_kanali,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net_onceki
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :onceki_bas AND tarih < :bas
        GROUP BY satis_kanali
    """), {"onceki_bas": onceki_bas, "bas": bas_str})
    _wow_map = {r["satis_kanali"]: float(r["net_onceki"] or 0) for r in onceki_rows.mappings()}

    # MoM: bir önceki ay aynı periyot
    mom_bas = str(bas_tarih - timedelta(days=30)) if son_gun else bas_str
    mom_son = str(bas_tarih - timedelta(days=1)) if son_gun else bas_str
    mom_rows = await session.execute(text("""
        SELECT satis_kanali,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net_mom
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :mom_bas AND tarih <= :mom_son
        GROUP BY satis_kanali
    """), {"mom_bas": mom_bas, "mom_son": mom_son})
    _mom_map = {r["satis_kanali"]: float(r["net_mom"] or 0) for r in mom_rows.mappings()}

    # Toplam WoW/MoM
    onceki_toplam = await session.execute(text("""
        SELECT SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :onceki_bas AND tarih < :bas
    """), {"onceki_bas": onceki_bas, "bas": bas_str})
    _prev_net = float((onceki_toplam.scalar()) or 0)
    mom_toplam = await session.execute(text("""
        SELECT SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :mom_bas AND tarih <= :mom_son
    """), {"mom_bas": mom_bas, "mom_son": mom_son})
    _mom_net = float((mom_toplam.scalar()) or 0)
    _cur_net = float(kpis.get("donem", {}).get("net") or 0)

    def _chg(cur, prv):
        return round((cur - prv) / abs(prv) * 100, 1) if prv else None

    kpis["donem"]["wow_pct"] = _chg(_cur_net, _prev_net)
    kpis["donem"]["mom_pct"] = _chg(_cur_net, _mom_net)

    for row in kanal_ozet:
        k = row.get("satis_kanali", "")
        net_now = float(row.get("net") or 0)
        net_wow = _wow_map.get(k, 0)
        net_mom = _mom_map.get(k, 0)
        row["wow_pct"] = _chg(net_now, net_wow)
        row["mom_pct"] = _chg(net_now, net_mom)

    # Bottom 10 ürünler
    bot_rows = await session.execute(text("""
        SELECT urun_kodu, MAX(urun_adi) AS urun_adi,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutar,0))) AS iade,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutar,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_ecommerce_gunluk
        WHERE tarih >= :bas AND satis_adet > 0
        GROUP BY urun_kodu
        HAVING SUM(satis_tutar) > 0
        ORDER BY net ASC
        LIMIT 10
    """), {"bas": bas_str})
    bottom_urunler = [dict(r) for r in bot_rows.mappings()]

    return {
        "son_gun": str(son_gun) if son_gun else None,
        "gun_sayisi": gun_sayisi,
        "kpis": kpis,
        "kanal_ozet": kanal_ozet,
        "trend": trend,
        "top_urunler": top_urunler,
        "bottom_urunler": bottom_urunler,
        "iade_matrisi": iade_matrisi,
        "ga4_ozet": ga4_ozet,
    }


async def get_adl_magaza(session: AsyncSession, gun_sayisi: int = 30) -> dict:
    """Mağaza Raporu: mağaza detay, top/bottom, trend."""
    try:
        await session.execute(text("SELECT 1 FROM incorta_magaza_gunluk LIMIT 1"))
    except Exception:
        await session.rollback()
        return {"hata": "magaza_veri_yok", "trend": [], "kpis": {}, "magaza_detay": [], "top_magaza": [], "bottom_magaza": []}
    max_r = await session.execute(text("SELECT MAX(tarih::date) FROM incorta_magaza_gunluk"))
    son_gun = max_r.scalar()
    if son_gun:
        bas_tarih = son_gun - timedelta(days=gun_sayisi - 1)
        bas_str     = str(bas_tarih)
        son_str     = str(son_gun)
        dun_str     = str(son_gun - timedelta(days=1))
        son_next    = str(son_gun + timedelta(days=1))
    else:
        bas_str = son_str = dun_str = son_next = str(date.today())

    # KPI
    kpi_rows = await session.execute(text("""
        SELECT
            CASE WHEN tarih >= :son AND tarih < :next THEN 'bugun'
                 WHEN tarih >= :dun AND tarih < :son  THEN 'dun'
                 ELSE 'donem' END AS donem,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutari,0))) AS iade,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutari,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutari,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct,
            COUNT(DISTINCT magaza) FILTER (WHERE magaza IS NOT NULL AND magaza <> '') AS aktif_magaza
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas
        GROUP BY donem
    """), {"bas": bas_str, "son": son_str, "next": son_next, "dun": dun_str})
    kpis: dict = {"bugun": {}, "dun": {}, "donem": {}}
    for r in kpi_rows.mappings():
        d = r["donem"]
        if d:
            kpis[d] = {k: float(v) if v is not None else 0.0 for k, v in r.items() if k != "donem"}

    # Trend
    trend_rows = await session.execute(text("""
        SELECT tarih::date AS gun,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutari,0))) AS iade,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutari,0)) AS net,
            SUM(satis_adet) AS adet
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas AND magaza IS NOT NULL AND magaza <> ''
        GROUP BY tarih::date
        ORDER BY tarih::date
    """), {"bas": bas_str})
    trend = [dict(r) for r in trend_rows.mappings()]

    # Tüm mağazalar (30 — net sıralı) — OBF proxy dahil
    magaza_rows = await session.execute(text("""
        SELECT magaza,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutari,0))) AS iade,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutari,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutari,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct,
            ROUND((SUM(satis_tutar)/NULLIF(SUM(SUM(satis_tutar))OVER(),0)*100)::numeric,1) AS pazar_payi,
            ROUND(((SUM(satis_tutar)+SUM(COALESCE(iade_tutari,0)))/NULLIF(SUM(satis_adet),0))::numeric,0) AS net_obf
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas AND magaza IS NOT NULL AND magaza <> ''
        GROUP BY magaza
        ORDER BY net DESC
        LIMIT 30
    """), {"bas": bas_str})
    magazalar = [dict(r) for r in magaza_rows.mappings()]

    # Zincir geneli OBF + kritik sayım
    zincir_obf = 0.0
    if magazalar:
        toplam_net  = sum(float(m.get("net") or 0) for m in magazalar)
        toplam_adet = sum(float(m.get("adet") or 0) for m in magazalar)
        zincir_obf  = round(toplam_net / max(toplam_adet, 1), 0)
    kritik_mag_sayisi = sum(1 for m in magazalar if float(m.get("iade_pct") or 0) > 25)
    kpis["donem"]["net_obf"]            = zincir_obf
    kpis["donem"]["kritik_magaza_sayisi"] = kritik_mag_sayisi

    # Top 5 ve Kritik 5 ayrı listeler
    top5_magazalar    = magazalar[:5]
    # Kritik = en yüksek iade oranına sahip 5 mağaza (minimum 5K ₺ net)
    kritik5_magazalar = sorted(
        [m for m in magazalar if float(m.get("net") or 0) > 5000],
        key=lambda m: float(m.get("iade_pct") or 0),
        reverse=True
    )[:5]

    # Top ürünler
    urun_rows = await session.execute(text("""
        SELECT urun_kodu, MAX(urun_adi) AS urun_adi,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutari,0))) AS iade,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutari,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutari,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas
        GROUP BY urun_kodu
        ORDER BY net DESC
        LIMIT 20
    """), {"bas": bas_str})
    top_urunler = [dict(r) for r in urun_rows.mappings()]

    # Bottom mağazalar (en düşük net — kritik liste)
    bottom_mag_rows = await session.execute(text("""
        SELECT magaza,
            SUM(satis_tutar) AS satis,
            ABS(SUM(COALESCE(iade_tutari,0))) AS iade,
            SUM(satis_tutar)+SUM(COALESCE(iade_tutari,0)) AS net,
            SUM(satis_adet) AS adet,
            ROUND((ABS(SUM(COALESCE(iade_tutari,0)))/NULLIF(SUM(satis_tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_magaza_gunluk
        WHERE tarih >= :bas AND magaza IS NOT NULL AND magaza <> ''
        GROUP BY magaza
        ORDER BY net ASC
        LIMIT 10
    """), {"bas": bas_str})
    bottom_magazalar = [dict(r) for r in bottom_mag_rows.mappings()]

    return {
        "son_gun":            str(son_gun) if son_gun else None,
        "gun_sayisi":         gun_sayisi,
        "kpis":               kpis,
        "trend":              trend,
        "magazalar":          magazalar,
        "top5_magazalar":     top5_magazalar,
        "kritik5_magazalar":  kritik5_magazalar,
        "bottom_magazalar":   bottom_magazalar,
        "top_urunler":        top_urunler,
        "zincir_obf":         zincir_obf,
    }


async def get_adl_premium(session: AsyncSession, ay_count: int = 3) -> dict:
    """Premium Marka Sağlığı: aylık brand health, kategori mix, sezon."""
    min_yyay = _min_yyay(ay_count)

    # Marka bazlı aylık KPI (from incorta_satis + pim_products)
    marka_rows = await session.execute(text("""
        SELECT COALESCE(p.marka_adi, 'Diğer') AS marka,
            s.yil, s.ay,
            SUM(s.tutar)                                         AS satis,
            ABS(COALESCE(SUM(d.tutar), 0))                       AS iade,
            SUM(s.tutar) + COALESCE(SUM(d.tutar), 0)            AS net,
            SUM(s.adet::int)                                     AS adet,
            ROUND((ABS(COALESCE(SUM(d.tutar),0))/NULLIF(SUM(s.tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_satis s
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        LEFT JOIN incorta_depo_iade d
            ON s.urun_kodu=d.urun_kodu AND s.yil=d.yil AND s.ay=d.ay
           AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        WHERE (s.yil*100+s.ay) >= :min_yyay
        GROUP BY marka, s.yil, s.ay
        ORDER BY s.yil, s.ay, marka
    """), {"min_yyay": min_yyay})
    marka_trend = [dict(r) for r in marka_rows.mappings()]

    # Kategori mix (urun_grubu_adi)
    kat_rows = await session.execute(text("""
        SELECT COALESCE(p.urun_grubu_adi, 'Diğer') AS kategori,
            COALESCE(p.marka_adi, 'Diğer') AS marka,
            SUM(s.tutar)                  AS satis,
            ABS(COALESCE(SUM(d.tutar),0)) AS iade,
            SUM(s.tutar)+COALESCE(SUM(d.tutar),0) AS net,
            ROUND((SUM(s.tutar)/NULLIF(SUM(SUM(s.tutar))OVER(),0)*100)::numeric,1) AS pay
        FROM incorta_satis s
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        LEFT JOIN incorta_depo_iade d
            ON s.urun_kodu=d.urun_kodu AND s.yil=d.yil AND s.ay=d.ay
           AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        WHERE (s.yil*100+s.ay) >= :min_yyay
        GROUP BY kategori, marka
        ORDER BY net DESC
        LIMIT 20
    """), {"min_yyay": min_yyay})
    kategori_mix = [dict(r) for r in kat_rows.mappings()]

    # Sezon dağılımı
    sezon_rows = await session.execute(text("""
        SELECT COALESCE(p.sezon_adi, 'Diğer') AS sezon,
            SUM(s.tutar) AS satis,
            SUM(s.tutar)+COALESCE(SUM(d.tutar),0) AS net,
            ROUND((SUM(s.tutar)/NULLIF(SUM(SUM(s.tutar))OVER(),0)*100)::numeric,1) AS pay
        FROM incorta_satis s
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        LEFT JOIN incorta_depo_iade d
            ON s.urun_kodu=d.urun_kodu AND s.yil=d.yil AND s.ay=d.ay
           AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        WHERE (s.yil*100+s.ay) >= :min_yyay
        GROUP BY sezon
        ORDER BY net DESC
        LIMIT 15
    """), {"min_yyay": min_yyay})
    sezon_dagili = [dict(r) for r in sezon_rows.mappings()]

    # Özet KPIs (totals)
    ozet_rows = await session.execute(text("""
        SELECT
            SUM(s.tutar) AS toplam_satis,
            ABS(COALESCE(SUM(d.tutar),0)) AS toplam_iade,
            SUM(s.tutar)+COALESCE(SUM(d.tutar),0) AS toplam_net,
            SUM(s.adet::int) AS toplam_adet,
            ROUND((ABS(COALESCE(SUM(d.tutar),0))/NULLIF(SUM(s.tutar),0)*100)::numeric,1) AS iade_pct,
            COUNT(DISTINCT s.urun_kodu) AS urun_cesidi
        FROM incorta_satis s
        LEFT JOIN incorta_depo_iade d
            ON s.urun_kodu=d.urun_kodu AND s.yil=d.yil AND s.ay=d.ay
           AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        WHERE (s.yil*100+s.ay) >= :min_yyay
    """), {"min_yyay": min_yyay})
    ozet = dict(ozet_rows.mappings().one_or_none() or {})

    # Beden dağılımı
    beden_rows = await session.execute(text("""
        SELECT beden,
            SUM(s.adet::int) AS satilan_adet,
            ROUND((SUM(s.adet::int) / NULLIF(SUM(SUM(s.adet::int))OVER(), 0) * 100)::numeric, 1) AS pay_pct
        FROM incorta_satis s
        WHERE (s.yil*100+s.ay) >= :min_yyay AND s.adet > 0 AND s.beden IS NOT NULL AND s.beden <> ''
        GROUP BY beden
        ORDER BY satilan_adet DESC
        LIMIT 12
    """), {"min_yyay": min_yyay})
    beden_dagilim = [dict(r) for r in beden_rows.mappings()]

    # Fiyat segmenti (Entry/Core/Premium/Luxury — ortalama birim fiyat bazlı)
    fiyat_seg_rows = await session.execute(text("""
        SELECT
            CASE
                WHEN (s.tutar / NULLIF(s.adet, 0)) < 500   THEN 'Entry'
                WHEN (s.tutar / NULLIF(s.adet, 0)) < 1500  THEN 'Core'
                WHEN (s.tutar / NULLIF(s.adet, 0)) < 3500  THEN 'Premium'
                ELSE 'Luxury'
            END AS segment,
            SUM(s.tutar) AS ciro,
            SUM(s.adet::int) AS adet,
            ROUND((SUM(s.tutar) / NULLIF(SUM(SUM(s.tutar)) OVER (), 0) * 100)::numeric, 1) AS ciro_pay
        FROM incorta_satis s
        WHERE (s.yil*100+s.ay) >= :min_yyay AND s.adet > 0 AND s.tutar > 0
        GROUP BY 1
        ORDER BY MIN(s.tutar / NULLIF(s.adet, 0))
    """), {"min_yyay": min_yyay})
    fiyat_segmenti = [dict(r) for r in fiyat_seg_rows.mappings()]

    # TARGET_MIX karşılaştırması — kategori_mix'e hedef ve sapma ekle
    _TARGET_MIX: dict = {
        "ELB": 35.0, "TRK": 20.0, "DGI": 15.0,
        "PNT": 10.0, "BLZ": 8.0, "AKS": 7.0,
    }
    _toplam_pay = sum(float(k.get("pay") or 0) for k in kategori_mix)
    for k in kategori_mix:
        kat_kod = (k.get("kategori") or "")[:3].upper()
        hedef   = _TARGET_MIX.get(kat_kod, 5.0)
        gercek  = float(k.get("pay") or 0)
        k["hedef_pct"] = hedef
        k["sapma"]     = round(gercek - hedef, 1)
        k["sapma_alarm"] = abs(gercek - hedef) >= 10
    ort_sapma = round(
        sum(abs(k["sapma"]) for k in kategori_mix[:8]) / max(len(kategori_mix[:8]), 1), 1
    )

    # Brand health index (skill formülü — mv_tam_fiyat_orani olmadan kısmi)
    iade_pct_val    = float(ozet.get("iade_pct") or 0)
    # Markdown disiplini: iade oranı proxy, <10% → 100, >35% → 0
    markdown_skoru  = max(0.0, 100.0 - max(0.0, iade_pct_val - 10.0) * 3.0)
    # Kategori dengesi: TARGET_MIX sapmasına göre
    kat_skoru       = max(0.0, 100.0 - ort_sapma * 5.0)
    # Placeholders (veri bekliyor)
    tam_fiyat_sk    = 65.0   # mv_tam_fiyat_orani gelmeyene kadar
    sezon_skoru     = 65.0   # mv_sell_through gelmeyene kadar
    marka_skoru     = 70.0   # Google Trends entegrasyonu bekliyor
    brand_health_total = round(
        tam_fiyat_sk * 0.30 + markdown_skoru * 0.20 +
        sezon_skoru  * 0.20 + kat_skoru      * 0.15 +
        marka_skoru  * 0.15, 1
    )
    brand_health = {
        "total":                brand_health_total,
        "tam_fiyat_skoru":      tam_fiyat_sk,
        "markdown_disiplini":   round(markdown_skoru, 1),
        "sezon_yenileme":       sezon_skoru,
        "kategori_dengesi":     round(kat_skoru, 1),
        "marka_sinyal":         marka_skoru,
        "ort_kategori_sapma":   ort_sapma,
        "rating": (
            "Güçlü"        if brand_health_total >= 80
            else "Sağlıklı" if brand_health_total >= 65
            else "İzleme"   if brand_health_total >= 50
            else "Risk Altında"
        ),
        "placeholder_not": "tam_fiyat ve sezon_skoru mv_tam_fiyat_orani / mv_sell_through bekleniyor",
    }

    # Premium + Luxury ciro payı (hedef >%50)
    prem_lux_pay = sum(
        float(s.get("ciro_pay") or 0)
        for s in fiyat_segmenti
        if s.get("segment") in ("Premium", "Luxury")
    )

    return {
        "min_yyay":         min_yyay,
        "ay_count":         ay_count,
        "ozet":             ozet,
        "marka_trend":      marka_trend,
        "kategori_mix":     kategori_mix,
        "sezon_dagili":     sezon_dagili,
        "beden_dagilim":    beden_dagilim,
        "fiyat_segmenti":   fiyat_segmenti,
        "prem_lux_pay":     round(prem_lux_pay, 1),
        "brand_health":     brand_health,
    }


async def get_adl_urun_stok(session: AsyncSession, ay_count: int = 3) -> dict:
    """Ürün & Stok Stratejisi: top/bottom ürünler, risk matrisi, kategori."""
    min_yyay = _min_yyay(ay_count)

    # Özet KPIs
    ozet_rows = await session.execute(text("""
        SELECT
            SUM(s.tutar) AS toplam_satis,
            ABS(COALESCE(SUM(d.tutar),0)) AS toplam_iade,
            SUM(s.tutar)+COALESCE(SUM(d.tutar),0) AS toplam_net,
            SUM(s.adet::int) AS toplam_adet,
            COUNT(DISTINCT s.urun_kodu) AS urun_cesidi,
            ROUND((ABS(COALESCE(SUM(d.tutar),0))/NULLIF(SUM(s.tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_satis s
        LEFT JOIN incorta_depo_iade d
            ON s.urun_kodu=d.urun_kodu AND s.yil=d.yil AND s.ay=d.ay
           AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        WHERE (s.yil*100+s.ay) >= :min_yyay
    """), {"min_yyay": min_yyay})
    ozet = dict(ozet_rows.mappings().one_or_none() or {})

    # Top 20 ürünler by net ciro
    top_rows = await session.execute(text("""
        SELECT s.urun_kodu, MAX(s.urun_adi) AS urun_adi,
            COALESCE(MAX(p.urun_grubu_adi), 'Diğer') AS kategori,
            COALESCE(MAX(p.marka_adi), 'Diğer') AS marka,
            SUM(s.tutar) AS satis,
            ABS(COALESCE(SUM(d.tutar),0)) AS iade,
            SUM(s.tutar)+COALESCE(SUM(d.tutar),0) AS net,
            SUM(s.adet::int) AS adet,
            ROUND((ABS(COALESCE(SUM(d.tutar),0))/NULLIF(SUM(s.tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_satis s
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        LEFT JOIN incorta_depo_iade d
            ON s.urun_kodu=d.urun_kodu AND s.yil=d.yil AND s.ay=d.ay
           AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        WHERE (s.yil*100+s.ay) >= :min_yyay
        GROUP BY s.urun_kodu
        ORDER BY net DESC
        LIMIT 20
    """), {"min_yyay": min_yyay})
    top_urunler = [dict(r) for r in top_rows.mappings()]

    # İade risk matrisi (iade_pct > 20%, min satış 5000 TL)
    risk_rows = await session.execute(text("""
        SELECT s.urun_kodu, MAX(s.urun_adi) AS urun_adi,
            COALESCE(MAX(p.urun_grubu_adi), 'Diğer') AS kategori,
            COALESCE(MAX(p.marka_adi), 'Diğer') AS marka,
            SUM(s.tutar) AS satis,
            ABS(COALESCE(SUM(d.tutar),0)) AS iade,
            SUM(s.adet::int) AS brut_adet,
            ROUND((ABS(COALESCE(SUM(d.tutar),0))/NULLIF(SUM(s.tutar),0)*100)::numeric,1) AS iade_pct
        FROM incorta_satis s
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        LEFT JOIN incorta_depo_iade d
            ON s.urun_kodu=d.urun_kodu AND s.yil=d.yil AND s.ay=d.ay
           AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        WHERE (s.yil*100+s.ay) >= :min_yyay
        GROUP BY s.urun_kodu
        HAVING SUM(s.tutar) >= 5000
           AND ABS(COALESCE(SUM(d.tutar),0))/NULLIF(SUM(s.tutar),0) >= 0.20
        ORDER BY iade_pct DESC
        LIMIT 20
    """), {"min_yyay": min_yyay})
    risk_urunler = [dict(r) for r in risk_rows.mappings()]

    # Kategori performansı
    kat_rows = await session.execute(text("""
        SELECT COALESCE(p.urun_grubu_adi, 'Diğer') AS kategori,
            SUM(s.tutar) AS satis,
            ABS(COALESCE(SUM(d.tutar),0)) AS iade,
            SUM(s.tutar)+COALESCE(SUM(d.tutar),0) AS net,
            SUM(s.adet::int) AS adet,
            COUNT(DISTINCT s.urun_kodu) AS urun_sayisi,
            ROUND((ABS(COALESCE(SUM(d.tutar),0))/NULLIF(SUM(s.tutar),0)*100)::numeric,1) AS iade_pct,
            ROUND((SUM(s.tutar)/NULLIF(SUM(SUM(s.tutar))OVER(),0)*100)::numeric,1) AS pay
        FROM incorta_satis s
        LEFT JOIN pim_products p ON p.urun_kodu = s.urun_kodu
        LEFT JOIN incorta_depo_iade d
            ON s.urun_kodu=d.urun_kodu AND s.yil=d.yil AND s.ay=d.ay
           AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
        WHERE (s.yil*100+s.ay) >= :min_yyay
        GROUP BY kategori
        ORDER BY net DESC
        LIMIT 20
    """), {"min_yyay": min_yyay})
    kategori_perf = [dict(r) for r in kat_rows.mappings()]

    # Risk count for KPI
    risk_count = len(risk_urunler)

    # Beden dağılımı (son N ay)
    beden_rows = await session.execute(text("""
        SELECT beden,
            SUM(adet::int) AS satilan_adet,
            ROUND((SUM(adet::int) / NULLIF(SUM(SUM(adet::int))OVER(), 0) * 100)::numeric, 1) AS pay_pct
        FROM incorta_satis
        WHERE (yil*100+ay) >= :min_yyay AND adet > 0 AND beden IS NOT NULL AND beden <> ''
        GROUP BY beden
        ORDER BY satilan_adet DESC
        LIMIT 15
    """), {"min_yyay": min_yyay})
    beden_dagilim = [dict(r) for r in beden_rows.mappings()]

    # Renk top 15
    renk_rows = await session.execute(text("""
        SELECT renk,
            SUM(adet::int) AS satilan_adet,
            SUM(tutar) AS ciro,
            ROUND((SUM(adet::int) / NULLIF(SUM(SUM(adet::int))OVER(), 0) * 100)::numeric, 1) AS pay_pct
        FROM incorta_satis
        WHERE (yil*100+ay) >= :min_yyay AND adet > 0 AND renk IS NOT NULL AND renk <> ''
        GROUP BY renk
        ORDER BY satilan_adet DESC
        LIMIT 15
    """), {"min_yyay": min_yyay})
    renk_top15 = [dict(r) for r in renk_rows.mappings()]

    # Restock önerisi — son ay yüksek satış, 60 günlük öneri hesabı
    # (incorta_satis aylık granülarite; min_yyay tek ay = son ay)
    son_ay_yyay = _min_yyay(1)
    restock_rows = await session.execute(text("""
        SELECT s.urun_kodu, MAX(s.urun_adi) AS urun_adi,
            SUM(s.adet::int)                            AS son_ay_satis,
            ROUND(SUM(s.adet::int) / 30.0, 1)          AS gunluk_ort,
            ROUND(SUM(s.adet::int) / 30.0 * 60)        AS onerilen_siparis,
            SUM(s.tutar)                                AS ciro,
            ROUND((s.tutar / NULLIF(s.adet, 0))::numeric, 0) AS birim_fiyat
        FROM incorta_satis s
        WHERE (s.yil*100+s.ay) >= :son_ay_yyay AND s.adet > 0
        GROUP BY s.urun_kodu, s.tutar, s.adet
        HAVING SUM(s.adet::int) > 20
        ORDER BY SUM(s.adet::int) DESC
        LIMIT 20
    """), {"son_ay_yyay": son_ay_yyay})
    restock_onerileri = [dict(r) for r in restock_rows.mappings()]

    # Hızlı eriyen (dönem geneli — sezon hız sıralaması)
    hizli_rows = await session.execute(text("""
        SELECT urun_kodu, MAX(urun_adi) AS urun_adi,
            SUM(adet::int) AS satilan_adet,
            SUM(tutar) AS ciro
        FROM incorta_satis
        WHERE (yil*100+ay) >= :min_yyay AND adet > 0
        GROUP BY urun_kodu
        HAVING SUM(adet::int) > 20
        ORDER BY SUM(adet::int) DESC
        LIMIT 20
    """), {"min_yyay": min_yyay})
    hizli_eriyenler = [dict(r) for r in hizli_rows.mappings()]

    # ── Ürün Başarı Tahmini (yeni ürünler — ilk satış son 60 gün) ────────
    bas60_str = str(date.today() - timedelta(days=60))
    bas45_str = str(date.today() - timedelta(days=45))
    basari_rows = await session.execute(text("""
        WITH ilk_satis AS (
            SELECT urun_kodu, MAX(urun_adi) AS urun_adi,
                MIN(tarih::date) AS ilk_tarih,
                SUM(satis_adet) AS toplam_adet,
                SUM(satis_tutar)+SUM(COALESCE(iade_tutar,0))+SUM(COALESCE(iptal_tutar,0)) AS net,
                (CURRENT_DATE - MIN(tarih::date)) AS yasam_gunu
            FROM incorta_ecommerce_gunluk
            WHERE tarih >= :bas60
            GROUP BY urun_kodu
            HAVING MIN(tarih) >= :bas45
        ),
        ilk_hafta AS (
            SELECT g.urun_kodu,
                SUM(g.satis_adet) AS ilk_hafta_adet
            FROM incorta_ecommerce_gunluk g
            JOIN ilk_satis s ON g.urun_kodu = s.urun_kodu
            WHERE g.tarih::date BETWEEN s.ilk_tarih AND (s.ilk_tarih + INTERVAL '6 days')::date
            GROUP BY g.urun_kodu
        )
        SELECT i.urun_kodu, i.urun_adi, i.ilk_tarih, i.toplam_adet,
               ROUND(i.net::numeric, 0) AS net,
               i.yasam_gunu,
               COALESCE(h.ilk_hafta_adet, 0) AS ilk_hafta_adet
        FROM ilk_satis i
        LEFT JOIN ilk_hafta h ON i.urun_kodu = h.urun_kodu
        WHERE COALESCE(h.ilk_hafta_adet, 0) >= 3
        ORDER BY i.toplam_adet DESC
        LIMIT 15
    """), {"bas60": bas60_str, "bas45": bas45_str})
    basari_ham = [dict(r) for r in basari_rows.mappings()]

    def _projeksiyon(ilk_hafta: int, yasam_gunu: int) -> dict:
        sezon_hafta = max(0, int(yasam_gunu or 0) // 7)
        if sezon_hafta <= 4:
            faktor = 1.0
        elif sezon_hafta <= 8:
            faktor = 0.85
        elif sezon_hafta <= 12:
            faktor = 0.65
        else:
            faktor = 0.40
        kalan = max(0, 13 - sezon_hafta)
        return {
            "sezon_hafta": sezon_hafta,
            "faktor": faktor,
            "kalan_hafta": kalan,
            "projeksiyon_adet": round(ilk_hafta * kalan * faktor),
        }

    urun_basari_tahmini = []
    for r in basari_ham:
        proj = _projeksiyon(int(r.get("ilk_hafta_adet") or 0), int(r.get("yasam_gunu") or 0))
        urun_basari_tahmini.append({**r, **proj})

    return {
        "min_yyay":             min_yyay,
        "ay_count":             ay_count,
        "ozet":                 {**ozet, "risk_urun_sayisi": risk_count},
        "top_urunler":          top_urunler,
        "risk_urunler":         risk_urunler,
        "kategori_perf":        kategori_perf,
        "beden_dagilim":        beden_dagilim,
        "renk_top15":           renk_top15,
        "hizli_eriyenler":      hizli_eriyenler,
        "restock_onerileri":    restock_onerileri,
        "urun_basari_tahmini":  urun_basari_tahmini,
    }
