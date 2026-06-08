"""Enrichment Agent — PLM ürün kalite puanı, eksik alan ve satış etkisi analizi."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.llm_client import llm_client
from app.core.logging import get_logger
from app.services.reporting.utils.date_context import get_date_context

log = get_logger(__name__)

# ── Grade skalası ─────────────────────────────────────────────────────────────
GRADE_SKALA = """
A (90-100) : Mükemmel — tüm alanlar dolu, yayına hazır
B (70-89)  : İyi — temel eksikler var, hızlı düzeltilebilir
C (50-69)  : Orta — birden fazla kritik alan eksik
D (30-49)  : Zayıf — önemli bilgiler yok, satışa hazır değil
F (0-29)   : Kritik — temel bilgiler bile eksik
"""

SKOR_ALANLARI = """
Temel Bilgi  (0-30) : Ürün adı, açıklama, kategori, renk, beden
Kumaş Bilgisi (0-25) : Kumaş içerik %, bakım, kumaş tipi/kodu
Görsel        (0-25) : Görsel sayısı, çeşidi, manken/detay
Satış İçeriği (0-20) : E-ticaret etiketi, ürün notu, koleksiyon hikayesi
"""

# ── Sistem prompt ─────────────────────────────────────────────────────────────
ENRICHMENT_SYSTEM = """\
Sen Pimland'ın ürün veri kalitesi için Zenginleştirme Agent'ısın.
Aşağıdaki tüm konularda UZMANSIN:

  • Kalite Puanı (A-F)  — grade dağılımı, sezon karşılaştırması, hedef belirleme
  • Eksik Alan Analizi   — hangi alanlar en çok eksik, önceliklendirme
  • Alt Skor Breakdown   — Temel/Kumaş/Görsel/Satış İçeriği boyutları
  • Satış Etkisi         — kalite puanı yüksek ürünler daha mı çok satıyor?
  • Aksiyon Listesi      — önce hangi ürünler düzeltilmeli, kaç iş günü

## Grade Skalası
{grade_skala}

## Skor Alanları
{skor_alanlari}

## Yanıt kuralları
- Türkçe · ürün içerik ekibi tonu · somut aksiyon öner
- Grade'leri A/B/C/D/F olarak göster
- "Bu SKU'ların kalitesi düşük" yerine "X alanı eksik, şu şekilde düzeltilir" de
- İyileşme önceliklendirmesinde: yüksek satış × düşük grade = öncelik #1
- Veri yoksa: "Bu sezon için henüz puanlama yapılmamış"

## Aktif filtreler
{filtreler}

## Mevcut veri özeti
{veri_ozeti}

{ton_eki}
"""

TON_EKLI = {
    "teknik":    "Hangi alanlarda ne kadar iş var, kaç SKU etkileniyor? Somut sayılarla.",
    "yonetici":  "Önce tek cümle durum, sonra top-3 aksiyon. Yönetim özeti formatında.",
    "analitik":  "Kalite puanı ile satış performansı arasındaki ilişkiyi analiz et.",
}


# ── Veri çekiciler ────────────────────────────────────────────────────────────

async def _fetch_sezon_ozet(session: AsyncSession, sezon: Optional[str]) -> List[Dict]:
    """enrichment_season_summary tablosundan sezon bazlı özet."""
    where = "WHERE sezon_kodu = :sezon" if sezon else ""
    params = {"sezon": sezon} if sezon else {}
    try:
        rows = (await session.execute(text(f"""
            SELECT sezon_kodu, sezon_adi, toplam_urun,
                   ortalama_puan::float AS ort_puan,
                   grade_a, grade_b, grade_c, grade_d, grade_f,
                   top_sorunlar, scored_at
            FROM enrichment_season_summary
            {where}
            ORDER BY scored_at DESC LIMIT 10
        """), params)).mappings().all()
        return [
            {
                "sezon_kodu": r["sezon_kodu"],
                "sezon_adi": r["sezon_adi"],
                "toplam_urun": r["toplam_urun"],
                "ort_puan": float(r["ort_puan"]),
                "grade_dagilimi": {
                    "A": r["grade_a"], "B": r["grade_b"], "C": r["grade_c"],
                    "D": r["grade_d"], "F": r["grade_f"],
                },
                "yayina_hazir_pct": round(
                    (r["grade_a"] + r["grade_b"]) / max(r["toplam_urun"], 1) * 100, 1
                ),
                "kritik_pct": round(
                    (r["grade_d"] + r["grade_f"]) / max(r["toplam_urun"], 1) * 100, 1
                ),
                "top_sorunlar": r["top_sorunlar"][:5] if r["top_sorunlar"] else [],
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("enrichment.sezon_error", error=str(e))
        return []


async def _fetch_skor_breakdown(session: AsyncSession, sezon: Optional[str]) -> Dict:
    """Alt skor ortalamaları ve zayıf boyut analizi."""
    where = "WHERE sezon_kodu = :sezon" if sezon else ""
    params = {"sezon": sezon} if sezon else {}
    try:
        row = (await session.execute(text(f"""
            SELECT
                COUNT(*) AS toplam,
                ROUND(AVG(quality_score)::numeric, 1) AS ort_toplam,
                ROUND(AVG(score_temel_bilgi)::numeric, 1) AS ort_temel,
                ROUND(AVG(score_kumas_bilgi)::numeric, 1) AS ort_kumas,
                ROUND(AVG(score_gorsel)::numeric, 1) AS ort_gorsel,
                ROUND(AVG(score_satis_icerik)::numeric, 1) AS ort_satis,
                -- Doluluk oranları (max üzerinden)
                ROUND(AVG(score_temel_bilgi)::numeric / 30 * 100, 1) AS temel_pct,
                ROUND(AVG(score_kumas_bilgi)::numeric / 25 * 100, 1) AS kumas_pct,
                ROUND(AVG(score_gorsel)::numeric / 25 * 100, 1) AS gorsel_pct,
                ROUND(AVG(score_satis_icerik)::numeric / 20 * 100, 1) AS satis_pct
            FROM enrichment_quality {where}
        """), params)).mappings().first()
        if not row:
            return {}
        return {
            "toplam_sku": int(row["toplam"]),
            "ort_puan": float(row["ort_toplam"] or 0),
            "alt_skorlar": {
                "temel_bilgi":   {"ort": float(row["ort_temel"] or 0),  "doluluk_pct": float(row["temel_pct"] or 0),  "max": 30},
                "kumas_bilgi":   {"ort": float(row["ort_kumas"] or 0),  "doluluk_pct": float(row["kumas_pct"] or 0),  "max": 25},
                "gorsel":        {"ort": float(row["ort_gorsel"] or 0), "doluluk_pct": float(row["gorsel_pct"] or 0), "max": 25},
                "satis_icerik":  {"ort": float(row["ort_satis"] or 0),  "doluluk_pct": float(row["satis_pct"] or 0),  "max": 20},
            },
        }
    except Exception as e:
        log.warning("enrichment.skor_error", error=str(e))
        return {}


async def _fetch_en_kritik_urunler(session: AsyncSession, sezon: Optional[str]) -> List[Dict]:
    """Düşük grade + yüksek satış = öncelikli iyileştirme listesi."""
    where_eq = "AND eq.sezon_kodu = :sezon" if sezon else ""
    params: Dict[str, Any] = {}
    if sezon:
        params["sezon"] = sezon

    try:
        rows = (await session.execute(text(f"""
            WITH satis AS (
                SELECT urun_kodu, SUM(tutar) brut
                FROM incorta_satis
                WHERE yil IN (2025, 2026)
                GROUP BY urun_kodu
            )
            SELECT eq.urun_kodu, p.urun_adi, eq.sezon_kodu,
                   eq.quality_score, eq.quality_grade,
                   eq.score_temel_bilgi, eq.score_kumas_bilgi,
                   eq.score_gorsel, eq.score_satis_icerik,
                   eq.eksik_alanlar,
                   COALESCE(s.brut, 0) AS brut_ciro
            FROM enrichment_quality eq
            LEFT JOIN pim_products p ON p.urun_kodu = eq.urun_kodu
            LEFT JOIN satis s ON s.urun_kodu = eq.urun_kodu
            WHERE eq.quality_grade IN ('D', 'F') {where_eq}
            ORDER BY COALESCE(s.brut, 0) DESC, eq.quality_score ASC
            LIMIT 10
        """), params)).mappings().all()

        return [
            {
                "urun_kodu": r["urun_kodu"],
                "urun_adi": r["urun_adi"] or r["urun_kodu"],
                "sezon_kodu": r["sezon_kodu"],
                "grade": r["quality_grade"],
                "puan": r["quality_score"],
                "alt_skorlar": {
                    "temel": r["score_temel_bilgi"],
                    "kumas": r["score_kumas_bilgi"],
                    "gorsel": r["score_gorsel"],
                    "satis": r["score_satis_icerik"],
                },
                "eksik_alanlar": (r["eksik_alanlar"] or [])[:4],
                "brut_ciro": int(r["brut_ciro"]),
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("enrichment.kritik_error", error=str(e))
        return []


async def _fetch_satis_etkisi(session: AsyncSession, sezon: Optional[str]) -> List[Dict]:
    """Grade bazlı ortalama satış — kalite ile satış ilişkisi."""
    where_eq = "AND eq.sezon_kodu = :sezon" if sezon else ""
    params: Dict[str, Any] = {}
    if sezon:
        params["sezon"] = sezon

    try:
        rows = (await session.execute(text(f"""
            WITH satis AS (
                SELECT urun_kodu, SUM(tutar) brut, SUM(adet::int) adet
                FROM incorta_satis
                WHERE yil IN (2025, 2026)
                GROUP BY urun_kodu
            )
            SELECT eq.quality_grade,
                   COUNT(*) AS sku_sayisi,
                   ROUND(AVG(COALESCE(s.brut, 0))::numeric) AS ort_brut_ciro,
                   ROUND(AVG(COALESCE(s.adet, 0))::numeric, 1) AS ort_adet
            FROM enrichment_quality eq
            LEFT JOIN satis s ON s.urun_kodu = eq.urun_kodu
            {('WHERE TRUE ' + where_eq) if where_eq else ''}
            GROUP BY eq.quality_grade
            ORDER BY eq.quality_grade
        """), params)).mappings().all()

        return [
            {
                "grade": r["quality_grade"],
                "sku_sayisi": int(r["sku_sayisi"]),
                "ort_brut_ciro": int(r["ort_brut_ciro"]),
                "ort_adet": float(r["ort_adet"]),
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("enrichment.satis_etkisi_error", error=str(e))
        return []


# ── Ana fonksiyon ─────────────────────────────────────────────────────────────

async def run_enrichment_agent(
    session: AsyncSession,
    question: str,
    filters: Dict[str, Any],
    history: List[Dict[str, Any]],
    ton: str = "teknik",
) -> Dict[str, Any]:
    t0 = time.perf_counter()

    sezon = filters.get("sezon") or None

    sezon_ozet  = await _fetch_sezon_ozet(session, sezon)
    skor_bd     = await _fetch_skor_breakdown(session, sezon)
    kritik_list = await _fetch_en_kritik_urunler(session, sezon)
    satis_etki  = await _fetch_satis_etkisi(session, sezon)

    ctx: Dict[str, Any] = {
        "filtre":           {"sezon": sezon},
        "sezon_ozeti":      sezon_ozet,
        "skor_breakdown":   skor_bd,
        "oncelikli_duzenle": kritik_list,
        "grade_x_satis":    satis_etki,
    }

    filtreler_str = json.dumps(ctx["filtre"], ensure_ascii=False)
    veri_str      = json.dumps(ctx, ensure_ascii=False, indent=2)
    ton_eki       = TON_EKLI.get(ton, TON_EKLI["teknik"])

    system = get_date_context() + "\n\n" + ENRICHMENT_SYSTEM.format(
        grade_skala=GRADE_SKALA,
        skor_alanlari=SKOR_ALANLARI,
        filtreler=filtreler_str,
        veri_ozeti=veri_str,
        ton_eki=ton_eki,
    )

    answer = await llm_client.complete(
        system=system,
        user=question,
        max_tokens=800,
        temperature=0.3,
        history=history[-6:] if history else [],
    )

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    log.info("enrichment_agent.done", elapsed_ms=elapsed,
             sezon=sezon, toplam_sku=skor_bd.get("toplam_sku", 0))

    return {
        "answer":    answer,
        "elapsed_ms": elapsed,
        "agent":     "ENRICHMENT_AGENT",
        "a2a_signal": None,
        "veri_ozeti": {
            "toplam_sku": skor_bd.get("toplam_sku", 0),
            "ort_puan": skor_bd.get("ort_puan", 0),
        },
    }
