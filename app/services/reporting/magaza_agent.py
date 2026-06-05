"""Mağaza Satış Agent — adL fiziksel mağaza ağı satış analisti.

Kapsam: hedef gerçekleştirme, MDO, OBF, sepet, ziyaretçi.
Kaynak: mv_magaza_satis_ozet (hızlı) → incorta_magaza_performans (fallback).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm_client import llm_client
from app.core.logging import get_logger

log = get_logger(__name__)

# ── Sektör normları (gömülü — prompt'a enjekte edilir) ───────────────────────
SEKTOR_NORMLARI = """
MDO    : TR moda %12-18 | %16+ Mükemmel | %13-16 İyi | <%13 Kritik
OBF    : Yıllık enflasyon + %2-5 artış makul
Hedef  : >%100 hedefi aştı | %85-100 iyi | %80-85 risk | <%80 kritik
Sepet  : 2.5-3.5 sağlıklı aralık
"""

# ── Ton eklentileri (commentary_engine tarafından seçilir) ───────────────────
TON_EKLI = {
    "yonetici":    "Yanıtını önce tek cümle özet, sonra 3 madde ile tamamla. Rakamlar önde gelsin.",
    "operasyonel": "Her bulguya somut aksiyon önerisi ekle.",
    "analitik":    "Trend yönünü ve momentumu yorum yap.",
    "stratejik":   "Yıllar arası büyüme hikayesini bağla.",
}

# ── Sistem prompt ─────────────────────────────────────────────────────────────
MAGAZA_SYSTEM = """\
Sen Pimland'ın fiziksel mağaza satış analisti için AI asistanısın.
Sadece mağaza verisi: hedef gerçekleştirme, MDO, OBF, sepet, ziyaretçi.

## Kapsam dışı sinyaller
- E-ticaret sorusu gelirse: [KAPSAM_DIŞI: ETICARET_AGENT]
- Ürün kalitesi sorusu: [KAPSAM_DIŞI: ENRICHMENT_AGENT]
- Geçmiş dönem karşılaştırması: [A2A_GEREKLİ: KIYASLAMA_AGENT, <soru>]

## Yanıt kuralları
- Türkçe · yönetici tonu · jargon yok
- Sayılarda Türk formatı: 1.234.567 ₺ · %14,7
- "tahmini / yaklaşık / veriye göre" — kesinlik iddia etme
- Tablo/SQL/kolon adı asla gösterme
- Veri yoksa: "Bu konuda yeterli veri şu an mevcut değil"

## Sektör normları
{sektor_normlari}

## Aktif filtreler
{filtreler}

## Mevcut veri özeti
{veri_ozeti}

{ton_eki}
"""


# ── Veri çekici ───────────────────────────────────────────────────────────────

def _fv(v: Any) -> float:
    if v is None or str(v).strip() in ('--', ''):
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


async def _fetch_magaza_context(
    session: AsyncSession,
    yil: int,
    ay: Optional[int] = None,
    bolge: Optional[str] = None,
    magaza: Optional[str] = None,
) -> Dict[str, Any]:
    """Mağaza satış bağlamını DB'den çeker. View varsa hızlı yol."""

    # Filtre koşulları
    conds = ["yil = :yil", "magaza IS NOT NULL", "TRIM(magaza) <> ''"]
    params: Dict[str, Any] = {"yil": yil}

    if ay:
        conds.append("ay::integer = :ay")
        params["ay"] = ay
    if bolge:
        conds.append("bolge_muduru ILIKE :bolge")
        params["bolge"] = f"%{bolge}%"
    if magaza:
        conds.append("magaza ILIKE :magaza")
        params["magaza"] = f"%{magaza}%"

    where = " AND ".join(conds)

    # 1. mv_magaza_satis_ozet varsa hızlı yol
    try:
        r = await session.execute(text(f"""
            SELECT bolge_muduru, magaza,
                   SUM(toplam_hedef) AS hedef,
                   SUM(toplam_ciro)  AS ciro,
                   SUM(toplam_ziyaretci) AS ziy,
                   SUM(toplam_adet) AS adet,
                   AVG(ort_mdo) AS mdo,
                   AVG(ort_obf) AS obf,
                   AVG(hedef_oran)*100 AS hedef_oran
            FROM mv_magaza_satis_ozet
            WHERE {where}
            GROUP BY bolge_muduru, magaza
            ORDER BY ciro DESC
            LIMIT 50
        """), params)
        rows = r.mappings().all()
        source = "mv_magaza_satis_ozet"
    except Exception:
        # Fallback: ham tablo
        r = await session.execute(text(f"""
            SELECT bolge_muduru, magaza,
                   SUM(CASE WHEN hedef::text NOT IN ('--','') THEN hedef::float ELSE 0 END) AS hedef,
                   SUM(CASE WHEN net_ciro::text NOT IN ('--','') THEN net_ciro::float ELSE 0 END) AS ciro,
                   SUM(CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END) AS ziy,
                   SUM(CASE WHEN net_adet::text NOT IN ('--','') THEN net_adet::float ELSE 0 END) AS adet,
                   AVG(CASE WHEN mdo::text NOT IN ('--','') THEN mdo::float ELSE NULL END) AS mdo,
                   AVG(CASE WHEN obf::text NOT IN ('--','') THEN obf::float ELSE NULL END) AS obf
            FROM incorta_magaza_performans
            WHERE {where}
            GROUP BY bolge_muduru, magaza
            ORDER BY ciro DESC
            LIMIT 50
        """), params)
        rows = r.mappings().all()
        source = "incorta_magaza_performans"

    if not rows:
        return {"hata": "Veri bulunamadı", "kaynak": source}

    # Toplamlar
    toplam_hedef = sum(_fv(r["hedef"]) for r in rows)
    toplam_ciro  = sum(_fv(r["ciro"])  for r in rows)
    toplam_ziy   = sum(_fv(r["ziy"])   for r in rows)
    genel_oran   = round(toplam_ciro / toplam_hedef * 100, 1) if toplam_hedef else 0

    # Bölge özeti
    bolge_ozet: Dict[str, Any] = {}
    for r in rows:
        b = r["bolge_muduru"] or "Atanmamış"
        if b not in bolge_ozet:
            bolge_ozet[b] = {"hedef": 0.0, "ciro": 0.0, "ziy": 0.0, "n": 0}
        bolge_ozet[b]["hedef"] += _fv(r["hedef"])
        bolge_ozet[b]["ciro"]  += _fv(r["ciro"])
        bolge_ozet[b]["ziy"]   += _fv(r["ziy"])
        bolge_ozet[b]["n"]     += 1

    bolge_liste = []
    for b, d in sorted(bolge_ozet.items(), key=lambda x: -x[1]["ciro"]):
        oran = round(d["ciro"] / d["hedef"] * 100, 1) if d["hedef"] else 0
        bolge_liste.append({
            "bolge": b, "magaza_sayisi": d["n"],
            "ciro": round(d["ciro"]), "hedef": round(d["hedef"]),
            "hedef_oran_pct": oran,
        })

    # En iyi / en kötü 5 mağaza
    mag_sorted = sorted(rows, key=lambda r: _fv(r.get("hedef_oran") or r.get("ciro", 0)), reverse=True)
    en_iyi  = [{"magaza": r["magaza"], "bolge": r["bolge_muduru"],
                "ciro": round(_fv(r["ciro"])),
                "hedef_oran_pct": round(_fv(r.get("hedef_oran", 0)))}
               for r in mag_sorted[:5]]
    en_kotu = [{"magaza": r["magaza"], "bolge": r["bolge_muduru"],
                "ciro": round(_fv(r["ciro"])),
                "hedef_oran_pct": round(_fv(r.get("hedef_oran", 0)))}
               for r in mag_sorted[-5:]]

    # Ortalama MDO / OBF
    mdo_vals = [_fv(r.get("mdo", 0)) for r in rows if _fv(r.get("mdo", 0)) > 0]
    obf_vals = [_fv(r.get("obf", 0)) for r in rows if _fv(r.get("obf", 0)) > 0]
    ort_mdo  = round(sum(mdo_vals) / len(mdo_vals), 1) if mdo_vals else 0
    ort_obf  = round(sum(obf_vals) / len(obf_vals), 0) if obf_vals else 0

    # MDO segmentleri
    mukemmel = sum(1 for v in mdo_vals if v >= 16)
    iyi      = sum(1 for v in mdo_vals if 13 <= v < 16)
    kritik   = sum(1 for v in mdo_vals if v < 13)

    return {
        "donem": f"{yil}" + (f" Ay:{ay}" if ay else " YTD"),
        "filtre": {"yil": yil, "ay": ay, "bolge": bolge, "magaza": magaza},
        "toplam": {
            "magaza_sayisi": len(rows),
            "hedef": round(toplam_hedef),
            "net_ciro": round(toplam_ciro),
            "hedef_oran_pct": genel_oran,
            "ziyaretci": round(toplam_ziy),
            "ort_mdo_pct": ort_mdo,
            "ort_obf": int(ort_obf),
        },
        "mdo_dagilimi": {
            "mukemmel_16plus": mukemmel,
            "iyi_13_16": iyi,
            "kritik_13_alti": kritik,
        },
        "bolgeler": bolge_liste[:8],
        "en_iyi_5_magaza":  en_iyi,
        "en_kotu_5_magaza": en_kotu,
        "kaynak": source,
    }


# ── Ana fonksiyon ─────────────────────────────────────────────────────────────

async def run_magaza_agent(
    session: AsyncSession,
    question: str,
    filters: Dict[str, Any],
    history: List[Dict[str, Any]],
    ton: str = "yonetici",
) -> Dict[str, Any]:
    """Mağaza Satış Agent'ını çalıştır. Kullanıcı sorusuna veri + yorum döner."""
    t0 = time.perf_counter()

    yil    = int(filters.get("yil", 2026))
    ay_raw = filters.get("ay")
    ay     = int(ay_raw) if ay_raw else None
    bolge  = filters.get("bolge") or None
    magaza = filters.get("magaza") or None

    # Veriyi çek
    ctx = await _fetch_magaza_context(session, yil, ay, bolge, magaza)

    if "hata" in ctx:
        return {
            "answer": "Bu dönem için mağaza satış verisi henüz mevcut değil. "
                      "Lütfen farklı filtreler deneyin.",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            "agent": "MAGAZA_AGENT",
            "a2a_signal": None,
        }

    # Sistem prompt'u oluştur
    filtreler_str = json.dumps(ctx["filtre"], ensure_ascii=False)
    veri_str      = json.dumps(ctx, ensure_ascii=False, indent=2)
    ton_eki       = TON_EKLI.get(ton, TON_EKLI["yonetici"])

    system = MAGAZA_SYSTEM.format(
        sektor_normlari=SEKTOR_NORMLARI,
        filtreler=filtreler_str,
        veri_ozeti=veri_str,
        ton_eki=ton_eki,
    )

    # LLM çağrısı
    answer = await llm_client.complete(
        system=system,
        user=question,
        max_tokens=800,
        temperature=0.3,
        history=history[-6:] if history else [],
    )

    # A2A / kapsam dışı sinyal tespiti
    a2a = None
    if "[A2A_GEREKLİ:" in answer:
        import re
        m = re.search(r'\[A2A_GEREKLİ:\s*(\w+)[,\s]+([^\]]+)\]', answer)
        if m:
            a2a = {"hedef_agent": m.group(1), "soru": m.group(2).strip()}

    kapsam_disi = None
    if "[KAPSAM_DIŞI:" in answer:
        import re
        m = re.search(r'\[KAPSAM_DIŞI:\s*([^\]]+)\]', answer)
        if m:
            kapsam_disi = m.group(1).strip()

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    log.info("magaza_agent.done", elapsed_ms=elapsed, a2a=a2a)

    return {
        "answer":    answer,
        "elapsed_ms": elapsed,
        "agent":     "MAGAZA_AGENT",
        "a2a_signal": a2a,
        "kapsam_disi": kapsam_disi,
        "veri_ozeti": {
            "donem": ctx["donem"],
            "toplam": ctx["toplam"],
        },
    }
