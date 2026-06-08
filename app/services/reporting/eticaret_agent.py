"""E-Ticaret Satış Agent — ADL/LMB online kanallar, SKU performansı, iade analizi."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import decimal

from app.agent.llm_client import llm_client
from app.core.logging import get_logger
from app.services.reporting.utils.date_context import get_date_context


def _to_native(v: Any) -> Any:
    """Decimal → float, diğerleri olduğu gibi."""
    return float(v) if isinstance(v, decimal.Decimal) else v


def _row_to_dict(row) -> Dict[str, Any]:
    return {k: _to_native(v) for k, v in row.items()}

log = get_logger(__name__)

# ── Sektör normları ───────────────────────────────────────────────────────────
SEKTOR_NORMLARI = """
İade oranı  : TR moda e-ticaret %18-22 normal | >%30 kritik | <%15 mükemmel
İptal oranı : <%5 iyi | %5-10 dikkat | >%10 kritik
OBF (₺)     : Sepet büyüklüğü — yıl bazlı enflasyon + büyüme beklentisi
Trendyol    : Pazar lideri, yüksek hacim düşük marj — iade riski yüksek
ADL kanalı  : Kendi kanalı, daha yüksek marj, iade oranı genelde daha düşük
"""

# ── Ton eklentileri ───────────────────────────────────────────────────────────
TON_EKLI = {
    "operasyonel": "Her bulguya somut aksiyon önerisi ekle. Kısa ve net.",
    "analitik":    "Kanal-ürün-iade üçgenini analiz et. Trend yönünü belirt.",
    "stratejik":   "Büyüme fırsatı ve risk dengesini yönetim perspektifinden sun.",
    "yonetici":    "Önce tek cümle özet, sonra 3 madde. Sayılar önde gelsin.",
}

# ── Sistem prompt ─────────────────────────────────────────────────────────────
ETICARET_SYSTEM = """\
Sen Pimland'ın online satış kanalları için E-Ticaret Agent'ısın.
Aşağıdaki tüm konularda UZMANSIN ve doğrudan yanıt verirsin:

  • Executive / KPI  — net/brüt ciro, iade/iptal oranı, OBF, MoM değişim
  • Genel Bakış       — aylık trend, kanal dağılımı, büyüme ivmesi
  • Kategori Analizi  — ürün grubu bazında ciro/iade/pay
  • Ürün Performansı  — top/riskli ürünler, SKU detayı, iade oranı
  • İade Analizi      — kanal/ürün/beden bazında iade, sebep analizi
  • Renk Analizi      — renk bazında satış/iade dağılımı
  • Ürün Satış        — belirli SKU veya ürün grubu odaklı analiz

Kapsam: ADL ve LMB markalarının online kanalları —
Trendyol, ADL Web/App, HepsiBurada, Boyner, LovemyBody, TY ADL AZ, TY LMB AZ

## Kapsam dışı
- Fiziksel mağaza sorusu: [KAPSAM_DIŞI: MAGAZA_AGENT]
- Ürün kalite/zenginleştirme: [KAPSAM_DIŞI: ENRICHMENT_AGENT]
- Açık YoY (farklı yıllar karşılaştırması): [A2A_GEREKLİ: KIYASLAMA_AGENT, <soru>]

## Kanal bazlı iade risk bilgisi
Trendyol: iade riski en yüksek pazar
ADL Web / App: genelde daha düşük iade
HepsiBurada / Boyner: orta seviye iade riski

## Yanıt kuralları
- Türkçe · analitik ton · jargon yok
- Sayılarda Türk formatı: 1.234.567 ₺ · %18,4
- Kesinlik iddia etme — "veriye göre", "görünüyor" kullan
- Tablo/SQL/kolon adı asla gösterme
- Kanal adlarında kısaltma yok: TRENDYOL (TY değil), HEPSIBURADA (HB değil)
- Veri yoksa: "Bu dönem için yeterli veri mevcut değil"

## Sektör normları
{sektor_normlari}

## Aktif filtreler
{filtreler}

## Mevcut veri özeti
{veri_ozeti}

{ton_eki}
"""


# ── Yardımcı: float güvenli ───────────────────────────────────────────────────
def _fv(v: Any) -> float:
    if v is None: return 0.0
    try: return float(v)
    except Exception: return 0.0


# ── Veri çekiciler (paralel çalışır) ─────────────────────────────────────────

async def _fetch_kpi(session: AsyncSession, yil: int, ay: Optional[int],
                     kanal: Optional[str]) -> Dict[str, Any]:
    """Brüt/net ciro, iade, iptal, OBF."""
    where_parts = ["s.yil = :yil"]
    params: Dict[str, Any] = {"yil": yil}
    if ay:
        where_parts.append("s.ay = :ay"); params["ay"] = ay
    if kanal:
        where_parts.append("s.satis_kanali = :kanal"); params["kanal"] = kanal
    where = "WHERE " + " AND ".join(where_parts)

    try:
        r = (await session.execute(text(f"""
            SELECT COALESCE(SUM(s.tutar),0) AS brut, COALESCE(SUM(s.adet::int),0) AS brut_adet
            FROM incorta_satis s {where}
        """), params)).mappings().first()

        wd = where.replace("s.", "d.")
        rd = (await session.execute(text(f"""
            SELECT ABS(COALESCE(SUM(d.tutar),0)) AS iade, ABS(COALESCE(SUM(d.adet::int),0)) AS iade_adet
            FROM incorta_depo_iade d {wd}
        """), params)).mappings().first()

        wi = where.replace("s.", "i.")
        ri = (await session.execute(text(f"""
            SELECT ABS(COALESCE(SUM(i.tutar),0)) AS iptal, ABS(COALESCE(SUM(i.adet::int),0)) AS iptal_adet
            FROM incorta_iptal_siparis i {wi}
        """), params)).mappings().first()

        brut = _fv(r["brut"]); iade = _fv(rd["iade"]); iptal = _fv(ri["iptal"])
        net  = brut - iade - iptal
        brut_adet = int(r["brut_adet"])
        net_adet  = max(brut_adet - int(rd["iade_adet"]) - int(ri["iptal_adet"]), 1)

        return {
            "brut_ciro": round(brut),
            "iade_ciro": round(iade),
            "iptal_ciro": round(iptal),
            "net_ciro": round(net),
            "brut_adet": brut_adet,
            "net_adet": net_adet,
            "iade_oran_pct": round(iade / brut * 100, 1) if brut else 0,
            "iptal_oran_pct": round(iptal / brut * 100, 1) if brut else 0,
            "net_obf": round(net / net_adet),
        }
    except Exception as e:
        log.warning("eticaret.kpi_error", error=str(e))
        return {}


async def _fetch_kanal(session: AsyncSession, yil: int, ay: Optional[int]) -> List[Dict]:
    """Kanal bazlı brüt ciro + iade oranı."""
    where_parts = ["s.yil = :yil"]
    params: Dict[str, Any] = {"yil": yil}
    if ay:
        where_parts.append("s.ay = :ay"); params["ay"] = ay
    where_s = "WHERE " + " AND ".join(where_parts)
    where_d = where_s.replace("s.", "d.")

    try:
        rows = (await session.execute(text(f"""
            WITH sat AS (
                SELECT satis_kanali,
                       SUM(tutar) AS brut, SUM(adet::int) AS brut_adet,
                       ROUND((100.0*SUM(tutar)/NULLIF(SUM(SUM(tutar))OVER(),0))::numeric,1) AS pay
                FROM incorta_satis s {where_s}
                GROUP BY satis_kanali
            ),
            iad AS (
                SELECT satis_kanali, ABS(SUM(tutar)) AS iade
                FROM incorta_depo_iade d {where_d}
                GROUP BY satis_kanali
            )
            SELECT sat.satis_kanali, sat.brut, sat.brut_adet, sat.pay,
                   COALESCE(iad.iade, 0) AS iade,
                   ROUND((COALESCE(iad.iade,0)/NULLIF(sat.brut,0)*100)::numeric,1) AS iade_oran_pct
            FROM sat LEFT JOIN iad USING(satis_kanali)
            ORDER BY sat.brut DESC
            LIMIT 12
        """), params)).mappings().all()
        return [_row_to_dict(r) for r in rows]
    except Exception as e:
        log.warning("eticaret.kanal_error", error=str(e))
        return []


async def _fetch_trend(session: AsyncSession, yil: int, kanal: Optional[str]) -> List[Dict]:
    """Aylık brüt/net ciro trendi."""
    where_parts = ["s.yil = :yil"]
    params: Dict[str, Any] = {"yil": yil}
    if kanal:
        where_parts.append("s.satis_kanali = :kanal"); params["kanal"] = kanal
    where_s = "WHERE " + " AND ".join(where_parts)
    where_d = where_s.replace("s.", "d.")

    AY_ADI = {1:"Ocak",2:"Şubat",3:"Mart",4:"Nisan",5:"Mayıs",6:"Haziran",
              7:"Temmuz",8:"Ağustos",9:"Eylül",10:"Ekim",11:"Kasım",12:"Aralık"}
    try:
        rows = (await session.execute(text(f"""
            SELECT s.ay,
                   ROUND(SUM(s.tutar)::numeric) AS brut,
                   ROUND(ABS(COALESCE(SUM(d.tutar),0))::numeric) AS iade
            FROM incorta_satis s
            LEFT JOIN incorta_depo_iade d
                   ON s.urun_kodu=d.urun_kodu AND s.ay=d.ay AND s.yil=d.yil
                  AND s.satis_kanali=d.satis_kanali AND s.renk=d.renk AND s.beden=d.beden
            {where_s.replace('s.yil','s.yil')}
            GROUP BY s.ay ORDER BY s.ay
        """), params)).mappings().all()
        return [
            {"ay": r["ay"], "ay_adi": AY_ADI.get(r["ay"], str(r["ay"])),
             "brut_ciro": int(r["brut"]), "iade_ciro": int(r["iade"]),
             "net_ciro": int(r["brut"]) - int(r["iade"])}
            for r in rows
        ]
    except Exception as e:
        log.warning("eticaret.trend_error", error=str(e))
        return []


async def _fetch_top_urunler(session: AsyncSession, yil: int, ay: Optional[int],
                              kanal: Optional[str]) -> List[Dict]:
    """Top 10 ürün net ciro + iade oranı."""
    where_parts = ["s.yil = :yil"]
    params: Dict[str, Any] = {"yil": yil}
    if ay:
        where_parts.append("s.ay = :ay"); params["ay"] = ay
    if kanal:
        where_parts.append("s.satis_kanali = :kanal"); params["kanal"] = kanal
    where_s = "WHERE " + " AND ".join(where_parts)
    where_d = where_s.replace("s.", "d.")

    try:
        rows = (await session.execute(text(f"""
            WITH sat AS (
                SELECT urun_kodu, MAX(urun_adi) urun_adi,
                       SUM(tutar) brut, SUM(adet::int) brut_adet
                FROM incorta_satis s {where_s}
                GROUP BY urun_kodu
            ),
            iad AS (
                SELECT urun_kodu, ABS(SUM(tutar)) iade
                FROM incorta_depo_iade d {where_d}
                GROUP BY urun_kodu
            )
            SELECT sat.urun_kodu, sat.urun_adi,
                   ROUND(sat.brut::numeric) AS brut_ciro,
                   ROUND(COALESCE(iad.iade,0)::numeric) AS iade_ciro,
                   ROUND((sat.brut - COALESCE(iad.iade,0))::numeric) AS net_ciro,
                   ROUND((COALESCE(iad.iade,0)/NULLIF(sat.brut,0)*100)::numeric,1) AS iade_pct
            FROM sat LEFT JOIN iad USING(urun_kodu)
            ORDER BY net_ciro DESC LIMIT 10
        """), params)).mappings().all()
        return [
            {"sira": i+1, "urun_kodu": r["urun_kodu"], "urun_adi": r["urun_adi"],
             "brut_ciro": int(r["brut_ciro"]), "iade_ciro": int(r["iade_ciro"]),
             "net_ciro": int(r["net_ciro"]), "iade_pct": float(r["iade_pct"] or 0)}
            for i, r in enumerate(rows)
        ]
    except Exception as e:
        log.warning("eticaret.urun_error", error=str(e))
        return []


async def _fetch_riskli_urunler(session: AsyncSession, yil: int,
                                 ay: Optional[int], kanal: Optional[str]) -> List[Dict]:
    """Yüksek iade oranlı riskli ürünler (brüt > 50K ₺)."""
    where_parts = ["s.yil = :yil"]
    params: Dict[str, Any] = {"yil": yil}
    if ay:
        where_parts.append("s.ay = :ay"); params["ay"] = ay
    if kanal:
        where_parts.append("s.satis_kanali = :kanal"); params["kanal"] = kanal
    where_s = "WHERE " + " AND ".join(where_parts)
    where_d = where_s.replace("s.", "d.")

    try:
        rows = (await session.execute(text(f"""
            WITH sat AS (
                SELECT urun_kodu, MAX(urun_adi) urun_adi, SUM(tutar) brut
                FROM incorta_satis s {where_s}
                GROUP BY urun_kodu HAVING SUM(tutar) > 50000
            ),
            iad AS (
                SELECT urun_kodu, ABS(SUM(tutar)) iade
                FROM incorta_depo_iade d {where_d}
                GROUP BY urun_kodu
            )
            SELECT sat.urun_kodu, sat.urun_adi,
                   ROUND(sat.brut::numeric) AS brut_ciro,
                   ROUND((COALESCE(iad.iade,0)/sat.brut*100)::numeric,1) AS iade_pct
            FROM sat LEFT JOIN iad USING(urun_kodu)
            WHERE COALESCE(iad.iade,0)/sat.brut*100 > 20
            ORDER BY iade_pct DESC LIMIT 5
        """), params)).mappings().all()
        return [
            {"urun_kodu": r["urun_kodu"], "urun_adi": r["urun_adi"],
             "brut_ciro": int(r["brut_ciro"]), "iade_pct": float(r["iade_pct"] or 0)}
            for r in rows
        ]
    except Exception as e:
        log.warning("eticaret.riskli_error", error=str(e))
        return []


async def _fetch_kategori(session: AsyncSession, yil: int,
                           ay: Optional[int], kanal: Optional[str]) -> List[Dict]:
    """Ürün grubu bazında net ciro + iade oranı."""
    where_parts = ["s.yil = :yil"]
    params: Dict[str, Any] = {"yil": yil}
    if ay:
        where_parts.append("s.ay = :ay"); params["ay"] = ay
    if kanal:
        where_parts.append("s.satis_kanali = :kanal"); params["kanal"] = kanal
    where_s = "WHERE " + " AND ".join(where_parts)
    where_d = where_s.replace("s.", "d.")

    try:
        rows = (await session.execute(text(f"""
            WITH sat AS (
                SELECT urun_kodu, SUM(tutar) brut, SUM(adet::int) adet
                FROM incorta_satis s {where_s} GROUP BY urun_kodu
            ),
            iad AS (
                SELECT urun_kodu, ABS(SUM(tutar)) iade
                FROM incorta_depo_iade d {where_d} GROUP BY urun_kodu
            )
            SELECT
                COALESCE(p.urun_grubu_adi, 'Diğer') AS kategori,
                ROUND(SUM(sat.brut)::numeric) AS brut_ciro,
                ROUND(COALESCE(SUM(iad.iade),0)::numeric) AS iade_ciro,
                ROUND((SUM(sat.brut)-COALESCE(SUM(iad.iade),0))::numeric) AS net_ciro,
                ROUND((100.0*SUM(sat.brut)/SUM(SUM(sat.brut))OVER())::numeric,1) AS pay,
                ROUND((COALESCE(SUM(iad.iade),0)/NULLIF(SUM(sat.brut),0)*100)::numeric,1) AS iade_pct
            FROM sat
            LEFT JOIN iad  USING(urun_kodu)
            LEFT JOIN pim_products p ON p.urun_kodu = sat.urun_kodu
            GROUP BY COALESCE(p.urun_grubu_adi,'Diğer')
            ORDER BY net_ciro DESC LIMIT 10
        """), params)).mappings().all()
        return [
            {"kategori": r["kategori"], "brut_ciro": int(r["brut_ciro"]),
             "iade_ciro": int(r["iade_ciro"]), "net_ciro": int(r["net_ciro"]),
             "pay_pct": float(r["pay"] or 0), "iade_pct": float(r["iade_pct"] or 0)}
            for r in rows
        ]
    except Exception as e:
        log.warning("eticaret.kategori_error", error=str(e))
        return []


async def _fetch_analytics(session: AsyncSession, yil: int, ay: Optional[int]) -> Dict[str, Any]:
    """Web analytics özeti: oturum, conversion, en iyi kaynak."""
    where_parts = ["EXTRACT(YEAR FROM date) = :yil"]
    params: Dict[str, Any] = {"yil": yil}
    if ay:
        where_parts.append("EXTRACT(MONTH FROM date) = :ay"); params["ay"] = ay
    where = "WHERE " + " AND ".join(where_parts)

    try:
        ozet = (await session.execute(text(f"""
            SELECT
                SUM(oturumlar) AS toplam_oturum,
                SUM(kullanicilar) AS toplam_kullanici,
                SUM(islem_sayisi) AS toplam_islem,
                ROUND(AVG(conversion_rate)::numeric*100, 2) AS ort_conversion_pct,
                ROUND(AVG(hemen_cikma_orani)::numeric*100, 1) AS ort_bounce_pct
            FROM incorta_analytics {where}
        """), params)).mappings().first()

        kaynak_rows = (await session.execute(text(f"""
            SELECT oturum_kaynagi,
                   SUM(oturumlar) AS oturum,
                   ROUND(AVG(conversion_rate)::numeric*100, 2) AS conversion_pct
            FROM incorta_analytics {where}
            GROUP BY oturum_kaynagi
            ORDER BY SUM(oturumlar) DESC LIMIT 5
        """), params)).mappings().all()

        return {
            "toplam_oturum":       int(ozet["toplam_oturum"] or 0),
            "toplam_kullanici":    int(ozet["toplam_kullanici"] or 0),
            "toplam_islem":        int(ozet["toplam_islem"] or 0),
            "ort_conversion_pct":  float(ozet["ort_conversion_pct"] or 0),
            "ort_bounce_pct":      float(ozet["ort_bounce_pct"] or 0),
            "trafik_kaynaklari":   [_row_to_dict(r) for r in kaynak_rows],
        }
    except Exception as e:
        log.warning("eticaret.analytics_error", error=str(e))
        return {}


# ── Ana fonksiyon ─────────────────────────────────────────────────────────────

async def run_eticaret_agent(
    session: AsyncSession,
    question: str,
    filters: Dict[str, Any],
    history: List[Dict[str, Any]],
    ton: str = "analitik",
) -> Dict[str, Any]:
    """E-Ticaret Agent — tüm veri katmanlarını paralel çekip LLM'e gönderir."""
    t0 = time.perf_counter()

    yil    = int(filters.get("yil", 2026))
    ay_raw = filters.get("ay")
    ay     = int(ay_raw) if ay_raw else None
    kanal  = filters.get("satiskanali") or filters.get("kanal") or None

    # Sıralı çek (asyncpg tek bağlantıda paralel sorgu desteklemiyor)
    def _safe(v): return v if not isinstance(v, Exception) else {}

    kpi          = await _fetch_kpi(session, yil, ay, kanal)
    kanal_ozeti  = await _fetch_kanal(session, yil, ay)
    trend        = await _fetch_trend(session, yil, kanal)
    top_urun     = await _fetch_top_urunler(session, yil, ay, kanal)
    riskli       = await _fetch_riskli_urunler(session, yil, ay, kanal)
    kategori     = await _fetch_kategori(session, yil, ay, kanal)
    analytics    = await _fetch_analytics(session, yil, ay)

    ctx: Dict[str, Any] = {
        "donem":       f"{yil}" + (f" Ay:{ay}" if ay else " YTD"),
        "filtre":      {"yil": yil, "ay": ay, "kanal": kanal},
        "kpi":         _safe(kpi),
        "kanal_ozeti": _safe(kanal_ozeti) if isinstance(kanal_ozeti, list) else [],
        "aylik_trend": _safe(trend) if isinstance(trend, list) else [],
        "top_10_urun": _safe(top_urun) if isinstance(top_urun, list) else [],
        "riskli_urunler": _safe(riskli) if isinstance(riskli, list) else [],
        "kategori_ozeti": _safe(kategori) if isinstance(kategori, list) else [],
        "web_analytics":  _safe(analytics),
    }

    filtreler_str = json.dumps(ctx["filtre"], ensure_ascii=False)
    veri_str      = json.dumps(ctx, ensure_ascii=False, indent=2)
    ton_eki       = TON_EKLI.get(ton, TON_EKLI["analitik"])

    system = get_date_context() + "\n\n" + ETICARET_SYSTEM.format(
        sektor_normlari=SEKTOR_NORMLARI,
        filtreler=filtreler_str,
        veri_ozeti=veri_str,
        ton_eki=ton_eki,
    )

    answer = await llm_client.complete(
        system=system,
        user=question,
        max_tokens=900,
        temperature=0.3,
        history=history[-6:] if history else [],
    )

    # A2A sinyal tespiti
    a2a = None
    if "[A2A_GEREKLİ:" in answer:
        import re
        m = re.search(r'\[A2A_GEREKLİ:\s*(\w+)[,\s]+([^\]]+)\]', answer)
        if m:
            a2a = {"hedef_agent": m.group(1), "soru": m.group(2).strip()}

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    log.info("eticaret_agent.done", elapsed_ms=elapsed, a2a=a2a,
             donem=ctx["donem"], kanal_sayisi=len(ctx["kanal_ozeti"]))

    return {
        "answer":      answer,
        "elapsed_ms":  elapsed,
        "agent":       "ETICARET_AGENT",
        "a2a_signal":  a2a,
        "veri_ozeti":  {"donem": ctx["donem"], "kpi": ctx["kpi"]},
    }
