"""Mağaza Satış Agent — adL fiziksel mağaza ağı satış analisti.

Kapsam: hedef gerçekleştirme, MDO, OBF, sepet, ziyaretçi.
Kaynak: mv_magaza_satis_ozet (hızlı) → incorta_magaza_performans (fallback).
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm_client import llm_client
from app.core.logging import get_logger
from app.services.reporting.utils.date_context import get_date_context

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
Sen Pimland'ın fiziksel mağaza ağı için Satış Agent'ısın.
Aşağıdaki tüm konularda UZMANSIN ve doğrudan yanıt verirsin:

  • Yönetici Özeti    — ağ geneli hedef gerçekleşme, MDO, OBF, bölge/mağaza sıralaması
  • Mağaza Performans — mağaza bazlı KPI karşılaştırması, segmentasyon, aksiyon listesi
  • Dönemsel Perf.   — ay bazlı ciro/hedef trendi, büyüme ivmesi, sezonsal dip/zirve
  • Dönemsel Karş.   — çeyrek/YTD karşılaştırması, en iyi/kötü dönem analizi

## Kapsam dışı
- E-ticaret sorusu: [KAPSAM_DIŞI: ETICARET_AGENT]
- Ürün kalite/zenginleştirme: [KAPSAM_DIŞI: ENRICHMENT_AGENT]
- Açık YoY (örn. "2024 ile 2025'i karşılaştır"): [A2A_GEREKLİ: KIYASLAMA_AGENT, <soru>]

## Doğrudan yanıtla — A2A tetikleme
magaza_tam_sira, bolgeler ve aylik_trend verileri mevcut; şunları devretme:
- Mağaza sıralaması, en iyi/kötü, hedefi aşan/aşmayan
- Aylık/çeyreklik performans trendi
- Bölge karşılaştırması
- MDO/OBF/sepet analizi

## Sıralama kuralı (ÖNEMLİ)
Kullanıcı "sıralama", "listele", "en iyi", "en kötü", "kaçıncı" gibi kelimeler kullandığında:
- magaza_tam_sira listesini kullan
- İlk 10-20 mağazayı tablo formatında göster (Sıra | Mağaza | Ciro | Hedef% | MDO)
- "dağılım" veya "özet" yerine MUTLAKA gerçek sıralama listesi ver

## Yanıt kuralları
- Türkçe · yönetici tonu · jargon yok
- Sayılarda Türk formatı: 1.234.567 ₺ · %14,7
- Kesinlik iddia etme — "veriye göre", "görünüyor" kullan
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

_AY_MAP = {
    "ocak": 1, "şubat": 2, "subat": 2, "mart": 3, "nisan": 4,
    "mayıs": 5, "mayis": 5, "haziran": 6, "temmuz": 7,
    "ağustos": 8, "agustos": 8, "eylül": 9, "eylul": 9,
    "ekim": 10, "kasım": 11, "kasim": 11, "aralık": 12, "aralik": 12,
}


def _extract_ay_from_question(question: str, ay: Optional[int]) -> Optional[int]:
    """Filtrede ay yoksa sorudan Türkçe ay adı veya rakam çıkarmaya çalış."""
    if ay:
        return ay
    q = question.lower()
    for k, v in _AY_MAP.items():
        if k in q:
            return v
    m = re.search(r'\bay\s*(\d{1,2})\b|\b(\d{1,2})[.\s]*ay\b', q)
    if m:
        n = int(m.group(1) or m.group(2))
        if 1 <= n <= 12:
            return n
    return None


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
            LIMIT 500
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
            LIMIT 500
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

    # Mağaza sıralaması — hedef gerçekleşme oranına göre (tüm liste, max 50)
    mag_sorted = sorted(rows, key=lambda r: _fv(r.get("hedef_oran") or 0), reverse=True)
    magaza_siralama = [
        {
            "sira": i + 1,
            "magaza": r["magaza"],
            "bolge": r["bolge_muduru"],
            "net_ciro": round(_fv(r["ciro"])),
            "hedef": round(_fv(r.get("hedef", 0))),
            "hedef_oran_pct": round(_fv(r.get("hedef_oran", 0)), 1),
            "mdo_pct": round(_fv(r.get("mdo", 0)) * 100, 1),
            "obf": round(_fv(r.get("obf", 0))),
            "ziyaretci": round(_fv(r["ziy"])),
        }
        for i, r in enumerate(mag_sorted)
    ]
    en_iyi  = magaza_siralama[:5]
    en_kotu = magaza_siralama[-5:]

    # Ziyaretçi bazlı sıralama (ayrıca)
    ziy_sorted = sorted(rows, key=lambda r: _fv(r["ziy"]), reverse=True)
    en_cok_ziyaret_5 = [
        {
            "sira": i + 1,
            "magaza": r["magaza"],
            "bolge": r["bolge_muduru"],
            "ziyaretci": round(_fv(r["ziy"])),
            "net_ciro": round(_fv(r["ciro"])),
            "hedef_oran_pct": round(_fv(r.get("hedef_oran", 0)), 1),
        }
        for i, r in enumerate(ziy_sorted[:5])
    ]

    # Ortalama MDO / OBF
    # mv_magaza_satis_ozet.ort_mdo fraksyon (0.07 = %7) → *100 ile yüzdeye çevir
    mdo_vals = [_fv(r.get("mdo", 0)) * 100 for r in rows if _fv(r.get("mdo", 0)) > 0]
    obf_vals = [_fv(r.get("obf", 0)) for r in rows if _fv(r.get("obf", 0)) > 0]
    ort_mdo  = round(sum(mdo_vals) / len(mdo_vals), 1) if mdo_vals else 0
    ort_obf  = round(sum(obf_vals) / len(obf_vals), 0) if obf_vals else 0

    # MDO segmentleri (eşikler yüzde cinsinden)
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
        "bolgeler": bolge_liste[:20],
        "en_iyi_5_magaza":  en_iyi,
        "en_kotu_5_magaza": en_kotu,
        "en_cok_ziyaret_5": en_cok_ziyaret_5,
        "magaza_tam_sira":  magaza_siralama,
        "kaynak": source,
    }


async def _fetch_aylik_trend(
    session: AsyncSession,
    yil: int,
) -> List[Dict[str, Any]]:
    """Yıl boyunca aylık ağ geneli ciro/hedef/ziyaretçi trendi."""
    AY_ADI = {1:"Ocak",2:"Şubat",3:"Mart",4:"Nisan",5:"Mayıs",6:"Haziran",
              7:"Temmuz",8:"Ağustos",9:"Eylül",10:"Ekim",11:"Kasım",12:"Aralık"}
    try:
        rows = (await session.execute(text("""
            SELECT ay::integer AS ay,
                   ROUND(SUM(CASE WHEN net_ciro::text  NOT IN ('--','') THEN net_ciro::float  ELSE 0 END)::numeric) AS ciro,
                   ROUND(SUM(CASE WHEN hedef::text     NOT IN ('--','') THEN hedef::float     ELSE 0 END)::numeric) AS hedef,
                   ROUND(SUM(CASE WHEN ziyaretci::text NOT IN ('--','') THEN ziyaretci::float ELSE 0 END)::numeric) AS ziyaretci,
                   ROUND((CASE
                        WHEN SUM(CASE WHEN hedef::text NOT IN ('--','') THEN hedef::float ELSE 0 END) > 0
                        THEN SUM(CASE WHEN net_ciro::text NOT IN ('--','') THEN net_ciro::float ELSE 0 END)
                             / SUM(CASE WHEN hedef::text NOT IN ('--','') THEN hedef::float ELSE 0 END) * 100
                        ELSE 0 END)::numeric, 1) AS hedef_oran_pct
            FROM incorta_magaza_performans
            WHERE yil = :yil AND magaza IS NOT NULL AND TRIM(magaza) <> ''
            GROUP BY ay::integer ORDER BY ay::integer
        """), {"yil": yil})).mappings().all()
        return [
            {
                "ay": r["ay"],
                "ay_adi": AY_ADI.get(r["ay"], str(r["ay"])),
                "ciro": int(r["ciro"]),
                "hedef": int(r["hedef"]),
                "ziyaretci": int(r["ziyaretci"]),
                "hedef_oran_pct": float(r["hedef_oran_pct"]),
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("magaza.trend_error", error=str(e))
        return []


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
    # Filtrede ay yoksa sorudan otomatik çıkar (örn. "Mayıs ayı sıralaması")
    ay     = _extract_ay_from_question(question, ay)
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

    # Aylık trend ekle (dönemsel bölümler için)
    ctx["aylik_trend"] = await _fetch_aylik_trend(session, yil)

    # Sistem prompt'u oluştur
    filtreler_str = json.dumps(ctx["filtre"], ensure_ascii=False)
    veri_str      = json.dumps(ctx, ensure_ascii=False, indent=2)
    ton_eki       = TON_EKLI.get(ton, TON_EKLI["yonetici"])

    system = get_date_context() + "\n\n" + MAGAZA_SYSTEM.format(
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
