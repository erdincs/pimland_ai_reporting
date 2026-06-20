"""Mağaza Satış — Yönetici Özeti dashboard (Incorta MCP)."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Query

from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/magaza-satis", tags=["magaza-satis"])

MCP_BASE = "https://agentup-mcp-test.pimland.com/30002"
TOOL_ID  = "post_api_v2_adl_dashboards_17589647_e5d1_40f7_81ce_a6ec_d66efa54"
_CACHE: Dict[str, Any] = {}
_CACHE_TTL = 1800  # 30 dk


def _fv(v) -> float:
    """None / '--' → 0.0"""
    if v is None or v == "--":
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


async def _fetch_from_db() -> List[List]:
    """incorta_magaza_performans tablosundan veri çek."""
    try:
        from app.db.session import ReadOnlySessionLocal
        from sqlalchemy import text as _text
        async with ReadOnlySessionLocal() as session:
            result = await session.execute(_text("""
                SELECT 
                    CASE WHEN yil::text ~ '^[0-9]+$' THEN yil::integer ELSE 0 END AS yil,
                    CASE WHEN ay::text  ~ '^[0-9.]+$' THEN ay::integer ELSE 0 END AS ay,
                    bolge_muduru,
                    magaza,
                    CASE WHEN hedef::text    IN ('--','') OR hedef IS NULL    THEN 0 ELSE hedef::float    END AS hedef,
                    CASE WHEN net_ciro::text IN ('--','') OR net_ciro IS NULL THEN 0 ELSE net_ciro::float END AS net_ciro,
                    CASE WHEN hedef_orani::text IN ('--','') OR hedef_orani IS NULL THEN 0 ELSE hedef_orani::float END AS hedef_orani,
                    CASE WHEN ziyaretci::text IN ('--','') OR ziyaretci IS NULL THEN 0 ELSE ziyaretci::float END AS ziyaretci,
                    CASE WHEN mdo::text IN ('--','') OR mdo IS NULL THEN 0 ELSE mdo::float END AS mdo,
                    CASE WHEN sepet::text IN ('--','') OR sepet IS NULL THEN 0 ELSE sepet::float END AS sepet,
                    CASE WHEN obf::text IN ('--','') OR obf IS NULL THEN 0 ELSE obf::float END AS obf,
                    CASE WHEN net_adet::text IN ('--','') OR net_adet IS NULL THEN 0 ELSE net_adet::float END AS net_adet
                FROM incorta_magaza_performans
                WHERE magaza IS NOT NULL AND TRIM(magaza) <> ''
                ORDER BY yil, ay, bolge_muduru, magaza
            """))
            rows = result.fetchall()
            if rows:
                # [yil, ay, bolge, magaza, hedef, ciro, oran, ziy, mdo, sepet, obf, adet]
                return [list(r) for r in rows]
    except Exception as e:
        log.warning("magaza_satis.db_error", error=str(e))
    return []


async def _fetch_mcp_data() -> List[List]:
    """Veriyi çek: DB varsa DB'den, yoksa MCP'den (cache'li)."""
    now = time.time()
    if _CACHE.get("data") and now - _CACHE.get("ts", 0) < _CACHE_TTL:
        return _CACHE["data"]

    # 1. DB'den dene
    db_rows = await _fetch_from_db()
    if db_rows:
        _CACHE.update({"data": db_rows, "ts": now})
        log.info("magaza_satis.from_db", rows=len(db_rows))
        return db_rows

    # 2. DB boşsa MCP'den çek (ilk sync veya fallback)
    token = os.environ.get("INCORTA_TOKEN", "")
    rows: List[List] = []

    PAGE = 500
    # Yıl filtresi: 2024-2026 arası veriyi zorla — Incorta sadece 2024 döndürürse fark edilir
    _INCORTA_YEAR_FILTER = [
        {
            "field": "SALES.DateHoursDim.Year",
            "operator": "BETWEEN",
            "values": [2024, 2026],
            "type": "dimension",
        }
    ]
    async with httpx.AsyncClient(timeout=60) as c:
        start = 0
        total = None
        while True:
            try:
                r = await c.post(
                    f"{MCP_BASE}/tools/{TOOL_ID}/execute",
                    json={"Authorization": token,
                          "pagination": {"startRow": start, "pageSize": PAGE},
                          "prompts": _INCORTA_YEAR_FILTER},
                )
                body = r.json()["content"]["data"]
                if total is None:
                    total = body.get("headers", {}).get("totalRows", 0)
                    log.info("magaza_satis.mcp_total", total=total)
                batch = body.get("data", [])
                if not batch:
                    break
                rows.extend(batch)
                start += PAGE
                if total and start >= total:
                    break
            except Exception as e:
                log.warning("magaza_satis.fetch_error", start=start, error=str(e))
                break

    # Mağaza adı boş olan satırlar MCP aggregate (toplam) satırları — çift sayımı önle
    rows = [r for r in rows if r[3] and str(r[3]).strip()]
    _CACHE.update({"data": rows, "ts": now})
    log.info("magaza_satis.from_mcp", rows=len(rows))
    return rows


# ── GET /yonetici-ozeti ────────────────────────────────────────────────────────

@router.get("/yonetici-ozeti")
async def get_yonetici_ozeti(
    yil: int  = Query(2026),
    ay:  Optional[int] = Query(None),  # None = YTD
) -> Dict[str, Any]:
    """Mağaza yönetici özeti — KPI, trend, bölge, insights."""

    all_rows = await _fetch_mcp_data()

    # Filtre
    rows = [r for r in all_rows if _fv(r[0]) == yil]
    if ay:
        rows = [r for r in rows if _fv(r[1]) == ay]

    if not rows:
        return {"error": "Veri bulunamadı"}

    # ── YTD toplamlar ─────────────────────────────────────────────────────────
    total_hedef  = sum(_fv(r[4]) for r in rows)
    total_ciro   = sum(_fv(r[5]) for r in rows)
    total_ziy    = sum(_fv(r[7]) for r in rows)
    total_adet   = sum(_fv(r[11]) for r in rows)
    mdo_w        = sum(_fv(r[8]) * _fv(r[7]) for r in rows)
    obf_w        = sum(_fv(r[10]) * _fv(r[11]) for r in rows)
    sepet_w      = sum(_fv(r[9]) * _fv(r[11]) for r in rows)

    ort_mdo   = mdo_w / total_ziy   if total_ziy  else 0
    ort_obf   = obf_w / total_adet  if total_adet else 0
    ort_sepet = sepet_w / total_adet if total_adet else 0
    hedef_oran = total_ciro / total_hedef if total_hedef else 0

    # ── Önceki yıl aynı dönem (YoY) ─────────────────────────────────────────
    prev_rows = [r for r in all_rows if _fv(r[0]) == yil - 1]
    if ay:
        prev_rows = [r for r in prev_rows if _fv(r[1]) == ay]
    else:
        # Sadece aynı ayları al (YTD)
        maks_ay = max(int(_fv(r[1])) for r in rows)
        prev_rows = [r for r in prev_rows if _fv(r[1]) <= maks_ay]

    prev_ciro   = sum(_fv(r[5]) for r in prev_rows)
    prev_ziy    = sum(_fv(r[7]) for r in prev_rows)
    prev_adet   = sum(_fv(r[11]) for r in prev_rows)
    prev_hedef_oran = (sum(_fv(r[5]) for r in prev_rows) /
                       max(sum(_fv(r[4]) for r in prev_rows), 1))
    prev_mdo_w  = sum(_fv(r[8]) * _fv(r[7]) for r in prev_rows)
    prev_obf_w  = sum(_fv(r[10]) * _fv(r[11]) for r in prev_rows)
    prev_ort_mdo  = prev_mdo_w / prev_ziy   if prev_ziy  else 0
    prev_ort_obf  = prev_obf_w / prev_adet  if prev_adet else 0

    # ── Aylık trend ──────────────────────────────────────────────────────────
    monthly: Dict[int, Dict] = {}
    for r in [x for x in all_rows if _fv(x[0]) == yil]:
        m = int(_fv(r[1]))
        if m not in monthly:
            monthly[m] = {"ay": m, "hedef": 0.0, "net_ciro": 0.0,
                          "ziyaretci": 0.0, "adet": 0.0}
        monthly[m]["hedef"]     += _fv(r[4])
        monthly[m]["net_ciro"]  += _fv(r[5])
        monthly[m]["ziyaretci"] += _fv(r[7])
        monthly[m]["adet"]      += _fv(r[11])

    ay_adlari = {1:"Oca",2:"Şub",3:"Mar",4:"Nis",5:"May",6:"Haz",
                 7:"Tem",8:"Ağu",9:"Eyl",10:"Eki",11:"Kas",12:"Ara"}
    trend = []
    for m in sorted(monthly.keys()):
        d = monthly[m]
        if d["net_ciro"] == 0:
            continue
        trend.append({
            "ay": d["ay"],
            "ay_adi": ay_adlari.get(d["ay"], str(d["ay"])),
            "hedef":    round(d["hedef"]),
            "net_ciro": round(d["net_ciro"]),
            "oran":     round(d["net_ciro"] / d["hedef"] * 100, 1) if d["hedef"] else 0,
        })

    # ── Bölge müdürü performansı ──────────────────────────────────────────────
    bolge_map: Dict[str, Dict] = {}
    bolge_magaza_map: Dict[str, Dict] = {}  # bolge -> magaza -> stats
    for r in rows:
        b = str(r[2]).strip() if r[2] and str(r[2]).strip() else None
        if not b:
            continue
        if b not in bolge_map:
            bolge_map[b] = {"bolge_muduru": b, "hedef": 0.0, "net_ciro": 0.0,
                            "ziyaretci": 0.0, "mdo_w": 0.0, "adet": 0.0,
                            "magaza_sayisi": 0}
            bolge_magaza_map[b] = {}
        d = bolge_map[b]
        d["hedef"]     += _fv(r[4])
        d["net_ciro"]  += _fv(r[5])
        d["ziyaretci"] += _fv(r[7])
        d["mdo_w"]     += _fv(r[8]) * _fv(r[7])
        d["adet"]      += _fv(r[11])
        d["magaza_sayisi"] += 1
        m = str(r[3]).strip() if r[3] else "?"
        if m not in bolge_magaza_map[b]:
            bolge_magaza_map[b][m] = {"magaza": m, "hedef": 0.0, "net_ciro": 0.0,
                                      "ziyaretci": 0.0, "mdo_w": 0.0}
        md = bolge_magaza_map[b][m]
        md["hedef"]     += _fv(r[4])
        md["net_ciro"]  += _fv(r[5])
        md["ziyaretci"] += _fv(r[7])
        md["mdo_w"]     += _fv(r[8]) * _fv(r[7])

    bolgeler = []
    for b, d in bolge_map.items():
        oran = d["net_ciro"] / d["hedef"] * 100 if d["hedef"] else 0
        mdo  = d["mdo_w"] / d["ziyaretci"] * 100 if d["ziyaretci"] else 0
        magaza_detay = []
        for md in sorted(bolge_magaza_map[b].values(), key=lambda x: -x["net_ciro"]):
            m_oran = md["net_ciro"] / md["hedef"] * 100 if md["hedef"] else 0
            m_mdo  = md["mdo_w"] / md["ziyaretci"] * 100 if md["ziyaretci"] else 0
            magaza_detay.append({
                "magaza":      md["magaza"],
                "hedef":       round(md["hedef"]),
                "net_ciro":    round(md["net_ciro"]),
                "hedef_oran":  round(m_oran, 1),
                "ziyaretci":   round(md["ziyaretci"]),
                "mdo":         round(m_mdo, 1),
            })
        bolgeler.append({
            **d,
            "hedef_oran":  round(oran, 1),
            "ort_mdo":     round(mdo, 1),
            "magaza_detay": magaza_detay,
        })
    bolgeler.sort(key=lambda x: -x["net_ciro"])

    # ── Mağaza bazlı (OBF sıralaması + MDO dağılımı) ─────────────────────────
    magaza_map: Dict[str, Dict] = {}
    for r in rows:
        m = str(r[3]).strip() if r[3] else "?"
        if m not in magaza_map:
            magaza_map[m] = {"magaza": m, "hedef": 0.0, "ciro": 0.0,
                             "ziy": 0.0, "mdo_w": 0.0, "obf_w": 0.0, "adet": 0.0}
        d = magaza_map[m]
        d["hedef"] += _fv(r[4]); d["ciro"] += _fv(r[5])
        d["ziy"]   += _fv(r[7]); d["adet"]  += _fv(r[11])
        d["mdo_w"] += _fv(r[8]) * _fv(r[7])
        d["obf_w"] += _fv(r[10]) * _fv(r[11])

    magaza_list = []
    mdo_dist = {"mukemmel": 0, "iyi": 0, "kritik": 0}
    for m, d in magaza_map.items():
        ort_mdo_m = d["mdo_w"] / d["ziy"] * 100 if d["ziy"] else 0
        ort_obf_m = d["obf_w"] / d["adet"] if d["adet"] else 0
        oran_m    = d["ciro"] / d["hedef"] * 100 if d["hedef"] else 0
        magaza_list.append({
            "magaza": m,
            "ciro": round(d["ciro"]), "hedef": round(d["hedef"]),
            "hedef_oran": round(oran_m, 1),
            "ort_mdo": round(ort_mdo_m, 1), "ort_obf": round(ort_obf_m, 0),
            "ziyaretci": round(d["ziy"]),
        })
        if ort_mdo_m >= 16:   mdo_dist["mukemmel"] += 1
        elif ort_mdo_m >= 13: mdo_dist["iyi"] += 1
        else:                 mdo_dist["kritik"] += 1

    obf_sirali = sorted(magaza_list, key=lambda x: -x["ort_obf"])
    top_obf = [{"magaza": m["magaza"], "obf": m["ort_obf"]} for m in obf_sirali[:5]]
    bot_obf = [{"magaza": m["magaza"], "obf": m["ort_obf"]} for m in obf_sirali[-3:]][::-1]

    hedef_asan = [m for m in magaza_list if m["hedef_oran"] >= 100]
    kritik     = [m for m in magaza_list if m["hedef_oran"] < 80 and m["hedef"] > 0]
    en_iyi_bolge  = max(bolgeler, key=lambda x: x["hedef_oran"]) if bolgeler else {}
    en_dusuk_bolge = min((b for b in bolgeler if b["hedef"] > 0),
                          key=lambda x: x["hedef_oran"], default={})

    # ── Önceki ay MDO (trend) ─────────────────────────────────────────────────
    maks_ay = max(int(_fv(r[1])) for r in rows) if rows else 1
    prev_ay_rows = [r for r in all_rows if _fv(r[0]) == yil and int(_fv(r[1])) == maks_ay - 1]
    prev_ay_mdo_w = sum(_fv(r[8]) * _fv(r[7]) for r in prev_ay_rows)
    prev_ay_ziy   = sum(_fv(r[7]) for r in prev_ay_rows)
    prev_ay_mdo   = prev_ay_mdo_w / prev_ay_ziy * 100 if prev_ay_ziy else 0

    # ── AI İçgörüleri ─────────────────────────────────────────────────────────
    insights = []
    if en_iyi_bolge:
        insights.append({
            "tur": "basari",
            "baslik": f"{en_iyi_bolge['bolge_muduru']} bölgesi lider",
            "aciklama": f"%{en_iyi_bolge['hedef_oran']} hedef gerçekleştirme oranı ile en iyi bölge. "
                        f"Ziyaretçi: {en_iyi_bolge['ziyaretci']:,.0f}.",
        })
    if en_dusuk_bolge and en_dusuk_bolge.get("hedef_oran", 100) < 80:
        insights.append({
            "tur": "risk",
            "baslik": f"{en_dusuk_bolge['bolge_muduru']} kritik seviyede",
            "aciklama": f"%{en_dusuk_bolge['hedef_oran']} oran ile en düşük bölge. "
                        f"Acil aksiyon planı gerekli.",
        })
    mdo_trend_delta = round(ort_mdo * 100 - prev_ay_mdo, 1)
    if abs(mdo_trend_delta) > 0.3:
        yon = "arttı" if mdo_trend_delta > 0 else "düştü"
        insights.append({
            "tur": "trend",
            "baslik": f"MDO {yon}",
            "aciklama": f"Bir önceki aya göre {abs(mdo_trend_delta):.1f}p değişim. "
                        f"Mevcut: %{ort_mdo*100:.1f}.",
        })
    obf_yoy = (ort_obf - prev_ort_obf) / prev_ort_obf * 100 if prev_ort_obf else 0
    if obf_yoy > 2:
        insights.append({
            "tur": "firsat",
            "baslik": f"OBF artışı devam ediyor",
            "aciklama": f"Birim fiyat YoY %{obf_yoy:.1f} arttı. Üst segment satışlar güçleniyor.",
        })
    ziy_yoy = (total_ziy - prev_ziy) / prev_ziy * 100 if prev_ziy else 0
    if ziy_yoy < -2:
        insights.append({
            "tur": "dikkat",
            "baslik": f"Ziyaretçi düşüşü",
            "aciklama": f"YoY %{abs(ziy_yoy):.1f} azalma. "
                        f"OBF artışı telafi ediyor ancak izlenmeli.",
        })
    if hedef_asan:
        insights.append({
            "tur": "basari",
            "baslik": f"{len(hedef_asan)} mağaza hedefi aştı",
            "aciklama": f"Toplam {len(magaza_list)} mağaza içinde {len(hedef_asan)} mağaza "
                        f"%100 üzerinde gerçekleştirme sağladı.",
        })

    # Sepet dağılımı (mock — gerçek veri olmadığı için oran hesabı)
    sepet_dist = [
        {"label": "1 ürün", "pct": 28},
        {"label": "2 ürün", "pct": 34},
        {"label": "3 ürün", "pct": 24},
        {"label": "4+",     "pct": 14},
    ]

    return {
        "donem": f"{yil}{f' · Ay {ay}' if ay else ' YTD'}",
        "kpis": {
            "hedef":      round(total_hedef),
            "net_ciro":   round(total_ciro),
            "hedef_oran": round(hedef_oran * 100, 1),
            "ziyaretci":  round(total_ziy),
            "mdo":        round(ort_mdo * 100, 2),
            "sepet":      round(ort_sepet, 2),
            "obf":        round(ort_obf, 0),
            "net_adet":   round(total_adet),
        },
        "yoy": {
            "net_ciro":   round((total_ciro - prev_ciro) / prev_ciro * 100, 1) if prev_ciro else 0,
            "hedef_oran": round(hedef_oran * 100 - prev_hedef_oran * 100, 1),
            "ziyaretci":  round(ziy_yoy, 1),
            "mdo":        round(mdo_trend_delta, 1),
            "obf":        round(obf_yoy, 1),
        },
        "hedef_kalan":    round(total_hedef - total_ciro),
        "gecen_yil_oran": round(prev_hedef_oran * 100, 1),
        "trend":          trend,
        "bolgeler":       bolgeler,
        "magaza_sayisi":  len(magaza_list),
        "hedef_asan_sayi": len(hedef_asan),
        "kritik_sayi":    len(kritik),
        "en_iyi_bolge":   en_iyi_bolge,
        "en_dusuk_bolge": en_dusuk_bolge,
        "mdo_dagilimi":   mdo_dist,
        "top_obf":        top_obf,
        "bot_obf":        bot_obf,
        "ort_obf":        round(ort_obf, 0),
        "sepet_dist":     sepet_dist,
        "insights":       insights[:6],
    }


# ── GET /magaza-performans ────────────────────────────────────────────────────

@router.get("/magaza-performans")
async def get_magaza_performans(
    yil:   int           = Query(2026),
    ay:    Optional[int] = Query(None),
    bolge: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Mağaza bazlı detay performans raporu."""

    all_rows = await _fetch_mcp_data()

    # Yıl + ay filtresi
    rows = [r for r in all_rows if _fv(r[0]) == yil]
    if ay:
        rows = [r for r in rows if _fv(r[1]) == ay]

    # Bölge filtresi
    if bolge and bolge != "tum":
        rows = [r for r in rows if str(r[2] or "").strip() == bolge]

    if not rows:
        return {"error": "Veri bulunamadı"}

    # ── Mağaza bazlı aggregation ──────────────────────────────────────────────
    mag_map: Dict[str, Dict] = {}
    mag_bolge: Dict[str, str] = {}

    for r in rows:
        mag  = str(r[3] or "").strip()
        bol  = str(r[2] or "").strip() or None
        if not mag:
            continue
        if mag not in mag_map:
            mag_map[mag] = {"magaza": mag, "hedef": 0.0, "ciro": 0.0, "ziy": 0.0,
                            "adet": 0.0, "mdo_w": 0.0, "obf_w": 0.0, "sepet_w": 0.0}
        d = mag_map[mag]
        d["hedef"]   += _fv(r[4]);  d["ciro"]    += _fv(r[5])
        d["ziy"]     += _fv(r[7]);  d["adet"]    += _fv(r[11])
        d["mdo_w"]   += _fv(r[8])  * _fv(r[7])
        d["obf_w"]   += _fv(r[10]) * _fv(r[11])
        d["sepet_w"] += _fv(r[9])  * _fv(r[11])
        if bol:
            mag_bolge[mag] = bol

    # Türetilmiş alanlar
    mağazalar = []
    for mag, d in mag_map.items():
        oran  = d["ciro"] / d["hedef"] * 100  if d["hedef"] else 0
        mdo   = d["mdo_w"]   / d["ziy"]   * 100 if d["ziy"]  else 0
        obf   = d["obf_w"]   / d["adet"]        if d["adet"] else 0
        sepet = d["sepet_w"] / d["adet"]         if d["adet"] else 0
        mağazalar.append({
            "magaza":       mag,
            "bolge_muduru": mag_bolge.get(mag, ""),
            "hedef":        round(d["hedef"]),
            "net_ciro":     round(d["ciro"]),
            "hedef_oran":   round(oran, 1),
            "ziyaretci":    round(d["ziy"]),
            "mdo":          round(mdo, 1),
            "sepet":        round(sepet, 2),
            "obf":          round(obf, 0),
            "net_adet":     round(d["adet"]),
        })

    mağazalar.sort(key=lambda x: -x["hedef_oran"])

    # ── Bölge müdürü kartları ─────────────────────────────────────────────────
    bolge_map: Dict[str, Dict] = {}
    bolge_mag_map: Dict[str, Dict] = {}
    for r in rows:
        b = str(r[2] or "").strip()
        if not b:
            continue
        if b not in bolge_map:
            bolge_map[b] = {"bolge_muduru": b, "hedef": 0.0, "ciro": 0.0,
                            "ziy": 0.0, "mdo_w": 0.0, "mag_set": set()}
            bolge_mag_map[b] = {}
        d2 = bolge_map[b]
        mag = str(r[3] or "").strip()
        d2["hedef"] += _fv(r[4]); d2["ciro"] += _fv(r[5])
        d2["ziy"]   += _fv(r[7]); d2["mdo_w"] += _fv(r[8]) * _fv(r[7])
        if mag:
            d2["mag_set"].add(mag)
            if mag not in bolge_mag_map[b]:
                bolge_mag_map[b][mag] = {"magaza": mag, "hedef": 0.0, "net_ciro": 0.0,
                                         "ziyaretci": 0.0, "mdo_w": 0.0}
            md = bolge_mag_map[b][mag]
            md["hedef"]     += _fv(r[4]); md["net_ciro"]  += _fv(r[5])
            md["ziyaretci"] += _fv(r[7]); md["mdo_w"]     += _fv(r[8]) * _fv(r[7])

    bolgeler = []
    for b, d2 in bolge_map.items():
        oran = d2["ciro"] / d2["hedef"] * 100 if d2["hedef"] else 0
        mdo  = d2["mdo_w"] / d2["ziy"] * 100  if d2["ziy"]  else 0
        magaza_detay = []
        for md in sorted(bolge_mag_map[b].values(), key=lambda x: -x["net_ciro"]):
            m_oran = md["net_ciro"] / md["hedef"] * 100 if md["hedef"] else 0
            m_mdo  = md["mdo_w"] / md["ziyaretci"] * 100 if md["ziyaretci"] else 0
            magaza_detay.append({
                "magaza":     md["magaza"],
                "hedef":      round(md["hedef"]),
                "net_ciro":   round(md["net_ciro"]),
                "hedef_oran": round(m_oran, 1),
                "ziyaretci":  round(md["ziyaretci"]),
                "mdo":        round(m_mdo, 1),
            })
        bolgeler.append({
            "bolge_muduru":  b,
            "magaza_sayisi": len(d2["mag_set"]),
            "hedef":         round(d2["hedef"]),
            "net_ciro":      round(d2["ciro"]),
            "hedef_oran":    round(oran, 1),
            "ziyaretci":     round(d2["ziy"]),
            "mdo":           round(mdo, 1),
            "magaza_detay":  magaza_detay,
        })
    bolgeler.sort(key=lambda x: -x["net_ciro"])

    # ── Performans sıralaması ─────────────────────────────────────────────────
    ranked = sorted(mağazalar, key=lambda x: -x["hedef_oran"])
    en_basarili = ranked[:3]
    aksiyon = [m for m in ranked if m["hedef_oran"] < 80]
    aksiyon.sort(key=lambda x: x["hedef_oran"])

    # Bölge listesi (dropdown için)
    bolge_listesi = sorted(bolge_map.keys())

    donem = f"{yil}{' · Ay ' + str(ay) if ay else ' YTD'}"

    return {
        "donem":          donem,
        "yil":            yil,
        "ay":             ay,
        "bolge":          bolge,
        "magaza_sayisi":  len(mağazalar),
        "bolgeler":       bolgeler,
        "bolge_listesi":  bolge_listesi,
        "mağazalar":      mağazalar,
        "en_basarili":    en_basarili,
        "aksiyon":        aksiyon[:5],
    }


# ── GET /donemseel-performans ─────────────────────────────────────────────────

@router.get("/donemseel-performans")
async def get_donemseel_performans(
    yil:   int           = Query(2026),
    bolge: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Aylık & haftalık dönemsel performans raporu."""
    import math

    all_rows = await _fetch_mcp_data()

    # Yıl + bölge filtresi
    rows = [r for r in all_rows if _fv(r[0]) == yil]
    if bolge and bolge != "tum":
        rows = [r for r in rows if str(r[2] or "").strip() == bolge]

    if not rows:
        return {"error": "Veri bulunamadı"}

    # ── Aylık aggregation ─────────────────────────────────────────────────────
    monthly: Dict[int, Dict] = {}
    for r in rows:
        m = int(_fv(r[1])) or 0
        if m == 0:
            continue
        if m not in monthly:
            monthly[m] = {"ay": m, "hedef": 0.0, "ciro": 0.0, "ziy": 0.0,
                          "adet": 0.0, "mdo_w": 0.0, "obf_w": 0.0, "sepet_w": 0.0}
        d2 = monthly[m]
        d2["hedef"]   += _fv(r[4]);  d2["ciro"]    += _fv(r[5])
        d2["ziy"]     += _fv(r[7]);  d2["adet"]    += _fv(r[11])
        d2["mdo_w"]   += _fv(r[8])  * _fv(r[7])
        d2["obf_w"]   += _fv(r[10]) * _fv(r[11])
        d2["sepet_w"] += _fv(r[9])  * _fv(r[11])

    ay_adlari = {1:"Ocak",2:"Şubat",3:"Mart",4:"Nisan",5:"Mayıs",6:"Haziran",
                 7:"Temmuz",8:"Ağustos",9:"Eylül",10:"Ekim",11:"Kasım",12:"Aralık"}

    aylik_liste = []
    for m, d2 in sorted(monthly.items()):
        if d2["ciro"] == 0:
            continue
        oran  = d2["ciro"] / d2["hedef"] * 100 if d2["hedef"] else 0
        mdo   = d2["mdo_w"]   / d2["ziy"]   * 100 if d2["ziy"]  else 0
        obf   = d2["obf_w"]   / d2["adet"]        if d2["adet"] else 0
        sepet = d2["sepet_w"] / d2["adet"]         if d2["adet"] else 0
        aylik_liste.append({
            "ay": m, "ay_adi": ay_adlari.get(m, str(m)),
            "hedef": round(d2["hedef"]), "net_ciro": round(d2["ciro"]),
            "hedef_oran": round(oran, 1), "ziyaretci": round(d2["ziy"]),
            "mdo": round(mdo, 1), "obf": round(obf, 0),
            "sepet": round(sepet, 2), "net_adet": round(d2["adet"]),
        })

    # ── YTD KPIs ──────────────────────────────────────────────────────────────
    ytd_hedef = sum(d2["hedef"] for d2 in monthly.values())
    ytd_ciro  = sum(d2["ciro"]  for d2 in monthly.values())
    ytd_ziy   = sum(d2["ziy"]   for d2 in monthly.values())
    ytd_adet  = sum(d2["adet"]  for d2 in monthly.values())
    ytd_mdo_w = sum(d2["mdo_w"] for d2 in monthly.values())
    ytd_oran  = ytd_ciro / ytd_hedef * 100 if ytd_hedef else 0
    ytd_mdo   = ytd_mdo_w / ytd_ziy * 100  if ytd_ziy   else 0

    # En iyi / en zayıf ay
    en_iyi  = max(aylik_liste, key=lambda x: x["hedef_oran"]) if aylik_liste else {}
    en_zayif = min(aylik_liste, key=lambda x: x["hedef_oran"]) if aylik_liste else {}

    # Önceki yıl YTD
    prev_rows = [r for r in all_rows if _fv(r[0]) == yil - 1]
    max_ay = max(monthly.keys()) if monthly else 12
    prev_rows = [r for r in prev_rows if _fv(r[1]) <= max_ay]
    prev_ciro = sum(_fv(r[5]) for r in prev_rows)
    prev_hedef = sum(_fv(r[4]) for r in prev_rows)
    prev_oran = prev_ciro / prev_hedef * 100 if prev_hedef else 0

    ciro_yoy  = (ytd_ciro - prev_ciro) / prev_ciro * 100   if prev_ciro  else 0
    oran_yoy  = ytd_oran - prev_oran

    # ── Son iki ay karşılaştırma ──────────────────────────────────────────────
    if len(aylik_liste) >= 2:
        prev_ay_d = aylik_liste[-2]
        curr_ay_d = aylik_liste[-1]
        delta = lambda k: (round((curr_ay_d[k]-prev_ay_d[k])/abs(prev_ay_d[k])*100,1)
                           if prev_ay_d.get(k) else 0)
        karsilastirma = {
            "onceki": prev_ay_d["ay_adi"],
            "simdiki": curr_ay_d["ay_adi"],
            "satirlar": [
                {"kpi":"Net Ciro",    "onceki":prev_ay_d["net_ciro"],"simdiki":curr_ay_d["net_ciro"],  "delta_pct":delta("net_ciro"),   "fmt":"tl"},
                {"kpi":"Hedef Oranı", "onceki":prev_ay_d["hedef_oran"],"simdiki":curr_ay_d["hedef_oran"],"delta_pct":round(curr_ay_d["hedef_oran"]-prev_ay_d["hedef_oran"],1),"fmt":"pct"},
                {"kpi":"Ziyaretçi",   "onceki":prev_ay_d["ziyaretci"],"simdiki":curr_ay_d["ziyaretci"], "delta_pct":delta("ziyaretci"),  "fmt":"sayi"},
                {"kpi":"MDO",         "onceki":prev_ay_d["mdo"],     "simdiki":curr_ay_d["mdo"],        "delta_pct":round(curr_ay_d["mdo"]-prev_ay_d["mdo"],1),"fmt":"pct"},
                {"kpi":"OBF",         "onceki":prev_ay_d["obf"],     "simdiki":curr_ay_d["obf"],        "delta_pct":delta("obf"),        "fmt":"tl"},
                {"kpi":"Sepet",       "onceki":prev_ay_d["sepet"],   "simdiki":curr_ay_d["sepet"],      "delta_pct":delta("sepet"),      "fmt":"num"},
            ]
        }
    else:
        karsilastirma = None

    # ── Haftalık ısı haritası (aylık veriden tahmin: 4 hafta + kısmi) ──────────
    from datetime import date as _date
    _today = _date.today()
    _cur_yil, _cur_ay = _today.year, _today.month
    _cur_hafta = (_today.day - 1) // 7   # 0-indexed: gün 1-7→0, 8-14→1, ...

    # Haftalık dağılım katsayıları — tekrar etmeden 5 ayrı değer
    _HAFTA_KATSAYILARI = [0.18, 0.22, 0.26, 0.20, 0.14]

    isi_harita = []
    for a in aylik_liste:
        hafta_sayisi = 5 if a["ay"] in [1, 3, 5, 7, 8, 10, 12] else 4
        is_current = (a.get("yil", yil) == _cur_yil and a["ay"] == _cur_ay)
        hafta_ciro = []
        for i in range(hafta_sayisi):
            if is_current and i > _cur_hafta:
                hafta_ciro.append(None)   # Henüz gelmemiş haftalar → "—"
            else:
                hafta_ciro.append(round(a["net_ciro"] * _HAFTA_KATSAYILARI[i], 0))
        isi_harita.append({
            "ay_adi": a["ay_adi"],
            "haftalar": hafta_ciro,
            "ort": round(a["net_ciro"] / hafta_sayisi, 0),
        })

    # ── Bölge kartları (top 3 by ciro) ────────────────────────────────────────
    bolge_map2: Dict[str, Dict] = {}
    bolge_mag_map2: Dict[str, Dict] = {}
    for r in rows:
        b = str(r[2] or "").strip()
        if not b:
            continue
        if b not in bolge_map2:
            bolge_map2[b] = {"bolge_muduru": b, "hedef": 0.0, "ciro": 0.0,
                             "ziy": 0.0, "mdo_w": 0.0, "mag_set": set()}
            bolge_mag_map2[b] = {}
        d3 = bolge_map2[b]
        d3["hedef"] += _fv(r[4]); d3["ciro"] += _fv(r[5])
        d3["ziy"]   += _fv(r[7]); d3["mdo_w"] += _fv(r[8]) * _fv(r[7])
        mag = str(r[3] or "").strip()
        if mag:
            d3["mag_set"].add(mag)
            if mag not in bolge_mag_map2[b]:
                bolge_mag_map2[b][mag] = {"magaza": mag, "hedef": 0.0, "net_ciro": 0.0,
                                          "ziyaretci": 0.0, "mdo_w": 0.0}
            md = bolge_mag_map2[b][mag]
            md["hedef"]     += _fv(r[4]); md["net_ciro"]  += _fv(r[5])
            md["ziyaretci"] += _fv(r[7]); md["mdo_w"]     += _fv(r[8]) * _fv(r[7])

    bolgeler = []
    for b, d3 in bolge_map2.items():
        oran = d3["ciro"] / d3["hedef"] * 100 if d3["hedef"] else 0
        mdo  = d3["mdo_w"] / d3["ziy"] * 100   if d3["ziy"]  else 0
        magaza_detay = []
        for md in sorted(bolge_mag_map2[b].values(), key=lambda x: -x["net_ciro"]):
            m_oran = md["net_ciro"] / md["hedef"] * 100 if md["hedef"] else 0
            m_mdo  = md["mdo_w"] / md["ziyaretci"] * 100 if md["ziyaretci"] else 0
            magaza_detay.append({
                "magaza":     md["magaza"],
                "hedef":      round(md["hedef"]),
                "net_ciro":   round(md["net_ciro"]),
                "hedef_oran": round(m_oran, 1),
                "ziyaretci":  round(md["ziyaretci"]),
                "mdo":        round(m_mdo, 1),
            })
        bolgeler.append({
            "bolge_muduru":  b,
            "magaza_sayisi": len(d3["mag_set"]),
            "hedef":         round(d3["hedef"]),
            "net_ciro":      round(d3["ciro"]),
            "hedef_oran":    round(oran, 1),
            "ziyaretci":     round(d3["ziy"]),
            "mdo":           round(mdo, 1),
            "magaza_detay":  magaza_detay,
        })
    bolgeler.sort(key=lambda x: -x["net_ciro"])

    bolge_listesi = sorted(bolge_map2.keys())

    return {
        "yil":         yil,
        "bolge":       bolge,
        "kpis": {
            "ytd_ciro":     round(ytd_ciro),
            "ytd_oran":     round(ytd_oran, 1),
            "ciro_yoy":     round(ciro_yoy, 1),
            "oran_yoy":     round(oran_yoy, 1),
            "en_iyi_ay":    en_iyi.get("ay_adi",""),
            "en_iyi_ciro":  en_iyi.get("net_ciro",0),
            "en_iyi_oran":  en_iyi.get("hedef_oran",0),
            "en_zayif_ay":  en_zayif.get("ay_adi",""),
            "en_zayif_ciro":en_zayif.get("net_ciro",0),
            "en_zayif_oran":en_zayif.get("hedef_oran",0),
        },
        "aylik":       aylik_liste,
        "isi_harita":  isi_harita,
        "karsilastirma": karsilastirma,
        "bolgeler":    bolgeler,
        "bolge_listesi": bolge_listesi,
    }


# ── GET /donemseel-karsilastirma ──────────────────────────────────────────────

@router.get("/donemseel-karsilastirma")
async def get_donemseel_karsilastirma() -> Dict[str, Any]:
    """2024-2025-2026 yılları arası dönemsel karşılaştırma raporu."""

    all_rows = await _fetch_mcp_data()

    def year_data(yil: int, max_ay: Optional[int] = None):
        rows = [r for r in all_rows if _fv(r[0]) == yil]
        if max_ay:
            rows = [r for r in rows if _fv(r[1]) <= max_ay]
        monthly: Dict[int, Dict] = {}
        for r in rows:
            m = int(_fv(r[1])) or 0
            if m == 0: continue
            if m not in monthly:
                monthly[m] = {"hedef": 0.0, "ciro": 0.0, "ziy": 0.0,
                              "adet": 0.0, "mdo_w": 0.0, "obf_w": 0.0, "sepet_w": 0.0}
            d = monthly[m]
            d["hedef"]   += _fv(r[4]); d["ciro"]  += _fv(r[5])
            d["ziy"]     += _fv(r[7]); d["adet"]  += _fv(r[11])
            d["mdo_w"]   += _fv(r[8])  * _fv(r[7])
            d["obf_w"]   += _fv(r[10]) * _fv(r[11])
            d["sepet_w"] += _fv(r[9])  * _fv(r[11])
        tot_ciro  = sum(d["ciro"]  for d in monthly.values())
        tot_hedef = sum(d["hedef"] for d in monthly.values())
        tot_ziy   = sum(d["ziy"]   for d in monthly.values())
        tot_adet  = sum(d["adet"]  for d in monthly.values())
        mdo_w     = sum(d["mdo_w"] for d in monthly.values())
        obf_w     = sum(d["obf_w"] for d in monthly.values())
        sep_w     = sum(d["sepet_w"] for d in monthly.values())
        return {
            "ciro":      round(tot_ciro),
            "hedef":     round(tot_hedef),
            "oran":      round(tot_ciro / tot_hedef * 100, 1) if tot_hedef else 0,
            "ziy":       round(tot_ziy),
            "adet":      round(tot_adet),
            "mdo":       round(mdo_w / tot_ziy * 100, 1) if tot_ziy else 0,
            "obf":       round(obf_w / tot_adet, 0) if tot_adet else 0,
            "sepet":     round(sep_w / tot_adet, 2) if tot_adet else 0,
            "monthly":   monthly,
            "aylar":     sorted(monthly.keys()),
        }

    d24 = year_data(2024)
    d25 = year_data(2025)
    # 2026 YTD: kaç ay var?
    d26_full = year_data(2026)
    ytd_aylar = d26_full["aylar"]
    ytd_ay_sayisi = len([a for a in ytd_aylar if d26_full["monthly"][a]["ciro"] > 0]) or 1

    # 2026 projection (full year estimate)
    pace = d26_full["ciro"] / ytd_ay_sayisi
    proj_ciro = round(pace * 12)
    proj_oran_delta = round(d26_full["oran"] - d25["oran"], 1)

    # YoY büyüme (karşılaştırılabilir dönem)
    def yoy(new_val, old_val):
        return round((new_val - old_val) / old_val * 100, 1) if old_val else 0

    # 2025 vs 2024 (aynı ay sayısı)
    common_ay_24_25 = [a for a in d25["aylar"] if a in d24["monthly"]]
    d24c = year_data(2024, max(common_ay_24_25) if common_ay_24_25 else 12)
    d25c = year_data(2025, max(common_ay_24_25) if common_ay_24_25 else 12)

    # 2026 vs 2025 (aynı YTD)
    d25_ytd = year_data(2025, ytd_ay_sayisi)

    yoy_data = {
        "net_ciro":  [yoy(d25c["ciro"], d24c["ciro"]),    yoy(d26_full["ciro"], d25_ytd["ciro"])],
        "ziyaretci": [yoy(d25c["ziy"],  d24c["ziy"]),     yoy(d26_full["ziy"],  d25_ytd["ziy"])],
        "mdo":       [round(d25c["mdo"]-d24c["mdo"],1),   round(d26_full["mdo"]-d25_ytd["mdo"],1)],
        "obf":       [yoy(d25c["obf"],  d24c["obf"]),     yoy(d26_full["obf"],  d25_ytd["obf"])],
        "sepet":     [yoy(d25c["sepet"],d24c["sepet"]),   yoy(d26_full["sepet"],d25_ytd["sepet"])],
        "net_adet":  [yoy(d25c["adet"], d24c["adet"]),    yoy(d26_full["adet"], d25_ytd["adet"])],
    }

    # Çeyreklik
    def quarter_sum(monthly, q_aylar):
        s = {"ciro": 0.0, "ziy": 0.0}
        for a in q_aylar:
            d = monthly.get(a, {})
            s["ciro"] += d.get("ciro", 0); s["ziy"] += d.get("ziy", 0)
        return round(s["ciro"]) if s["ciro"] > 0 else None

    QUARTERS = [(1,[1,2,3]),(2,[4,5,6]),(3,[7,8,9]),(4,[10,11,12])]
    Q_LABELS  = ["OCA-MAR","NİS-HAZ","TEM-EYL","EKİ-ARA"]
    ceyreklik = []
    for i,(qn,aylar) in enumerate(QUARTERS):
        c24 = quarter_sum(d24["monthly"], aylar)
        c25 = quarter_sum(d25["monthly"], aylar)
        c26 = quarter_sum(d26_full["monthly"], aylar)
        delta = yoy(c25, c24) if (c25 and c24) else None
        ceyreklik.append({
            "q": f"Q{qn}", "label": Q_LABELS[i],
            "y2024": c24, "y2025": c25, "y2026": c26,
            "delta_pct": delta,
            "bekliyor": c26 is None,
            "kismi": c26 is not None and len([a for a in aylar if a in d26_full["aylar"] and d26_full["monthly"].get(a,{}).get("ciro",0)>0]) < 3,
        })

    # Aylık karşılaştırma
    AY_ADLARI = {1:"Oca",2:"Şub",3:"Mar",4:"Nis",5:"May",6:"Haz",7:"Tem",8:"Ağu",9:"Eyl",10:"Eki",11:"Kas",12:"Ara"}
    aylik_trend = []
    for a in range(1, 13):
        aylik_trend.append({
            "ay": a, "ay_adi": AY_ADLARI[a],
            "y2024": round(d24["monthly"].get(a, {}).get("ciro", 0)) or None,
            "y2025": round(d25["monthly"].get(a, {}).get("ciro", 0)) or None,
            "y2026": round(d26_full["monthly"].get(a, {}).get("ciro", 0)) or None,
        })

    # Büyüme katkısı (2026 vs 2025 waterfall)
    obf_katki   = round(d26_full["ciro"] - d25_ytd["ciro"])
    ziy_katki   = round((d26_full["ziy"] - d25_ytd["ziy"]) * d25_ytd["obf"] / 1000 * d25_ytd["sepet"]) if d25_ytd["obf"] else 0
    mdo_katki   = round((d26_full["mdo"] - d25_ytd["mdo"]) / 100 * d25_ytd["ziy"] * d25_ytd["obf"] / 1000) if d25_ytd["ziy"] else 0
    sepet_katki = round((d26_full["sepet"] - d25_ytd["sepet"]) * d25_ytd["ziy"] * d26_full["mdo"] / 100) if d25_ytd["ziy"] else 0

    # Aylara göre 3 yıl yan yana karşılaştırma (filtre yok, tüm aylar)
    def month_kpis(yd, ay):
        m = yd["monthly"].get(ay, {})
        ciro  = m.get("ciro", 0)
        hedef = m.get("hedef", 0)
        ziy   = m.get("ziy", 0)
        adet  = m.get("adet", 0)
        if ciro == 0:
            return None
        return {
            "net_ciro":   round(ciro),
            "hedef":      round(hedef),
            "hedef_oran": round(ciro / hedef * 100, 1) if hedef else 0,
            "ziyaretci":  round(ziy),
            "mdo":        round(m["mdo_w"] / ziy * 100, 1) if ziy else 0,
            "obf":        round(m["obf_w"] / adet, 0) if adet else 0,
            "net_adet":   round(adet),
        }

    aylik_karsilastirma = []
    for a in range(1, 13):
        k24 = month_kpis(d24, a)
        k25 = month_kpis(d25, a)
        k26 = month_kpis(d26_full, a)
        if not any([k24, k25, k26]):
            continue
        aylik_karsilastirma.append({
            "ay": a, "ay_adi": AY_ADLARI[a],
            "y2024": k24,
            "y2025": k25,
            "y2026": k26,
        })

    return {
        "y2024":     d24,
        "y2025":     d25,
        "y2026_ytd": d26_full,
        "y2026_proj": {
            "ciro": proj_ciro,
            "oran_delta": proj_oran_delta,
            "aylar": ytd_ay_sayisi,
        },
        "yoy":       yoy_data,
        "ceyreklik": ceyreklik,
        "aylik_trend": aylik_trend,
        "aylik_karsilastirma": aylik_karsilastirma,
        "buyume_katkisi": {
            "baz_2025":  d25_ytd["ciro"],
            "proj_2026": proj_ciro,
            "obf":    round(d26_full["obf"]*d26_full["adet"]/1e6 - d25_ytd["obf"]*d25_ytd["adet"]/1e6)*1e6 if d25_ytd["adet"] else 0,
            "ziyaretci": round((d26_full["ziy"]-d25_ytd["ziy"])*d25_ytd["obf"]*d25_ytd["mdo"]/100) if d25_ytd["ziy"] else 0,
            "mdo":    mdo_katki,
            "sepet":  sepet_katki,
        },
    }
