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


async def _fetch_mcp_data() -> List[List]:
    """Tüm mağaza satış verisini MCP'den çek (cache'li)."""
    now = time.time()
    if _CACHE.get("data") and now - _CACHE.get("ts", 0) < _CACHE_TTL:
        return _CACHE["data"]

    token = os.environ.get("INCORTA_TOKEN", "")
    rows: List[List] = []

    async with httpx.AsyncClient(timeout=30) as c:
        for start in range(0, 4000, 500):
            try:
                r = await c.post(
                    f"{MCP_BASE}/tools/{TOOL_ID}/execute",
                    json={"Authorization": token,
                          "pagination": {"startRow": start, "pageSize": 500}},
                )
                batch = r.json()["content"]["data"].get("data", [])
                if not batch:
                    break
                rows.extend(batch)
            except Exception as e:
                log.warning("magaza_satis.fetch_error", start=start, error=str(e))
                break

    _CACHE.update({"data": rows, "ts": now})
    log.info("magaza_satis.fetched", rows=len(rows))
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
    for r in rows:
        b = str(r[2]).strip() if r[2] and str(r[2]).strip() else None
        if not b:
            continue
        if b not in bolge_map:
            bolge_map[b] = {"bolge_muduru": b, "hedef": 0.0, "net_ciro": 0.0,
                            "ziyaretci": 0.0, "mdo_w": 0.0, "adet": 0.0,
                            "magaza_sayisi": 0}
        d = bolge_map[b]
        d["hedef"]     += _fv(r[4])
        d["net_ciro"]  += _fv(r[5])
        d["ziyaretci"] += _fv(r[7])
        d["mdo_w"]     += _fv(r[8]) * _fv(r[7])
        d["adet"]      += _fv(r[11])
        d["magaza_sayisi"] += 1

    bolgeler = []
    for b, d in bolge_map.items():
        oran = d["net_ciro"] / d["hedef"] * 100 if d["hedef"] else 0
        mdo  = d["mdo_w"] / d["ziyaretci"] * 100 if d["ziyaretci"] else 0
        bolgeler.append({
            **d,
            "hedef_oran": round(oran, 1),
            "ort_mdo": round(mdo, 1),
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
        "bolgeler":       bolgeler[:10],
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
