"""SQL queries for the analytics portal.

All queries run on the read-only session and return plain dicts.
Multi-value filters (ay, kanal) use PostgreSQL ANY(:arr) binding.
"""

from __future__ import annotations

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

    return result
