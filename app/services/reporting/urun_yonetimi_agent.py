"""Ürün Yönetimi Agent — PLM katalog, sezon/tema/kategori analizi, satış performansı."""
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

_STOK_KEYWORDS = frozenset(["stok", "stok durumu", "stokta", "kaç adet", "mevcut mu", "var mı", "envanter"])


async def _fetch_stok_live(question: str) -> Optional[Dict[str, Any]]:
    """Soruda 10+ haneli ürün kodu ve stok anahtar kelimesi varsa MCP'den stok çek."""
    q_lower = question.lower()
    if not any(kw in q_lower for kw in _STOK_KEYWORDS):
        return None
    codes = re.findall(r'\b\d{10,}\b', question)
    if not codes:
        return None
    try:
        from app.connectors.pimland_live import fetch_product_full
        data = await fetch_product_full(codes[0])
        if not data:
            return None
        stocks = data.get("stocks") or []
        # Beden/renk bazlı özet
        stok_ozet = [
            {
                "renk": s.get("colorName") or s.get("colorCode"),
                "beden": s.get("sizeName") or s.get("sizeCode"),
                "mevcut": s.get("available", 0),
                "rezerv": s.get("reserved", 0),
                "toplam": s.get("total", 0),
            }
            for s in stocks[:30]
        ]
        return {
            "urun_kodu": codes[0],
            "toplam_mevcut": sum(s.get("available", 0) for s in stocks),
            "toplam_rezerv": sum(s.get("reserved", 0) for s in stocks),
            "beden_renk_stok": stok_ozet,
        }
    except Exception as exc:
        log.warning("urun_yonetimi.stok_error", error=str(exc))
        return None

# ── Sistem prompt ─────────────────────────────────────────────────────────────
URUN_YONETIMI_SYSTEM = """\
Sen Pimland'ın PLM (Ürün Yaşam Döngüsü) sistemi için Ürün Yönetimi Agent'ısın.
Aşağıdaki tüm konularda UZMANSIN ve doğrudan yanıt verirsin:

  • PLM Katalog        — toplam SKU, marka/sezon/tema/kategori dağılımı
  • Ürün Yönetimi YK  — yönetim kurulu özetleri, marka karşılaştırması, büyüme
  • Sezon Analizi      — cari/planlanan/arşiv sezonlar, YoY SKU büyümesi
  • Tema Performansı   — CORE/YENİ/CARRY tema analizi, kategori yoğunluğu
  • Kategori Analizi   — ürün grubu/ana grup dağılımı, derinlik analizi
  • Satış–PLM Köprüsü  — PLM ürünlerinin satış performansı, iade oranı, en iyi temalar
  • Katalog Sağlığı    — blokaj, internet aktivasyonu, eksik veri durumu
  • Stok Durumu        — Pimland MCP'den anlık stok: beden/renk bazlı mevcut/rezerv/toplam

Hiyerarşi: Marka → Sezon → Tema → Kategori (ürün grubu) → SKU

## Kapsam dışı
- Online kanal satış sorusu: [KAPSAM_DIŞI: ETICARET_AGENT]
- Fiziksel mağaza sorusu: [KAPSAM_DIŞI: MAGAZA_AGENT]
- Ürün kalite/zenginleştirme: [KAPSAM_DIŞI: ENRICHMENT_AGENT]

## Yanıt kuralları
- Türkçe · yönetim kurulu tonu · özet önce detay sonra
- Sayı formatı: 1.234 SKU, %14,7 pay
- Marka adları: "adL" (büyük L), "Love My Body"
- Sezon kodları: "26-SR" = 2026 Spring/Summer, "25-26 WR" = 2025-26 Winter
- Bloke ürün = katalogda var ama satışa kapalı
- İnternet aktif = web/app kanalında listelenen ürün
- Veri yoksa: "Bu filtre için yeterli PLM verisi mevcut değil"

## Aktif filtreler
{filtreler}

## Mevcut veri özeti
{veri_ozeti}

{ton_eki}
"""

TON_EKLI = {
    "yonetici": "Önce tek cümle özet, sonra en kritik 3 bulgu. YK formatında.",
    "analitik": "Sezon/tema/kategori üçgenini derinlemesine analiz et. Trend yönünü belirt.",
    "stratejik": "Portföy dengesini, büyüme fırsatlarını ve riskleri ortaya koy.",
}


# ── Veri çekiciler ────────────────────────────────────────────────────────────

async def _fetch_genel_ozet(
    session: AsyncSession,
    marka: Optional[str],
    sezon: Optional[str],
    tema: Optional[str],
) -> Dict[str, Any]:
    """Toplam SKU, marka dağılımı, katalog sağlığı."""
    conds = []
    params: Dict[str, Any] = {}
    if marka:
        conds.append("marka_adi = :marka"); params["marka"] = marka
    if sezon:
        conds.append("sezon_kodu = :sezon"); params["sezon"] = sezon
    if tema:
        conds.append("tema_adi = :tema"); params["tema"] = tema
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    try:
        r = (await session.execute(text(f"""
            SELECT
                COUNT(*) AS toplam_sku,
                COUNT(DISTINCT marka_adi) AS marka_sayisi,
                COUNT(DISTINCT sezon_kodu) AS sezon_sayisi,
                COUNT(DISTINCT tema_adi) AS tema_sayisi,
                COUNT(DISTINCT urun_grubu_adi) AS kategori_sayisi,
                SUM(CASE WHEN bloke = true THEN 1 ELSE 0 END) AS bloke_sku,
                SUM(CASE WHEN internet_aktif = true THEN 1 ELSE 0 END) AS internet_aktif_sku
            FROM pim_products {where}
        """), params)).mappings().first()

        marka_rows = (await session.execute(text(f"""
            SELECT marka_adi,
                   COUNT(*) AS sku,
                   COUNT(DISTINCT sezon_kodu) AS sezon,
                   COUNT(DISTINCT tema_adi) AS tema,
                   ROUND((100.0*COUNT(*)/SUM(COUNT(*))OVER())::numeric, 1) AS pay
            FROM pim_products {where}
            GROUP BY marka_adi ORDER BY sku DESC
        """), params)).mappings().all()

        return {
            "toplam_sku": int(r["toplam_sku"]),
            "marka_sayisi": int(r["marka_sayisi"]),
            "sezon_sayisi": int(r["sezon_sayisi"]),
            "tema_sayisi": int(r["tema_sayisi"]),
            "kategori_sayisi": int(r["kategori_sayisi"]),
            "bloke_sku": int(r["bloke_sku"]),
            "internet_aktif_sku": int(r["internet_aktif_sku"]),
            "bloke_oran_pct": round(int(r["bloke_sku"]) / max(int(r["toplam_sku"]), 1) * 100, 1),
            "marka_dagilimi": [
                {"marka": row["marka_adi"], "sku": int(row["sku"]),
                 "sezon": int(row["sezon"]), "tema": int(row["tema"]),
                 "pay_pct": float(row["pay"])}
                for row in marka_rows
            ],
        }
    except Exception as e:
        log.warning("urun_yonetimi.genel_error", error=str(e))
        return {}


async def _fetch_sezon_analiz(
    session: AsyncSession,
    marka: Optional[str],
    tema: Optional[str],
) -> List[Dict[str, Any]]:
    """Sezon bazlı SKU sayısı ve satış performansı."""
    conds = []
    params: Dict[str, Any] = {}
    if marka:
        conds.append("p.marka_adi = :marka"); params["marka"] = marka
    if tema:
        conds.append("p.tema_adi = :tema"); params["tema"] = tema
    where_p = ("WHERE " + " AND ".join(conds)) if conds else ""

    try:
        rows = (await session.execute(text(f"""
            WITH plm AS (
                SELECT sezon_kodu, marka_adi,
                       COUNT(*) AS sku,
                       COUNT(DISTINCT tema_adi) AS tema,
                       COUNT(DISTINCT urun_grubu_adi) AS kategori
                FROM pim_products p {where_p}
                GROUP BY sezon_kodu, marka_adi
            ),
            satis AS (
                SELECT p.sezon_kodu,
                       ROUND(SUM(s.tutar)::numeric) AS brut_ciro,
                       SUM(s.adet::int) AS brut_adet
                FROM incorta_satis s
                JOIN pim_products p ON p.urun_kodu = s.urun_kodu
                {where_p.replace('p.', 'p.')}
                GROUP BY p.sezon_kodu
            )
            SELECT plm.sezon_kodu, plm.marka_adi, plm.sku, plm.tema, plm.kategori,
                   COALESCE(satis.brut_ciro, 0) AS brut_ciro,
                   COALESCE(satis.brut_adet, 0) AS brut_adet
            FROM plm LEFT JOIN satis USING(sezon_kodu)
            ORDER BY plm.sezon_kodu DESC LIMIT 15
        """), params)).mappings().all()

        return [
            {
                "sezon_kodu": r["sezon_kodu"],
                "marka": r["marka_adi"],
                "sku_sayisi": int(r["sku"]),
                "tema_sayisi": int(r["tema"]),
                "kategori_sayisi": int(r["kategori"]),
                "brut_ciro": int(r["brut_ciro"]),
                "brut_adet": int(r["brut_adet"]),
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("urun_yonetimi.sezon_error", error=str(e))
        return []


async def _fetch_tema_analiz(
    session: AsyncSession,
    marka: Optional[str],
    sezon: Optional[str],
) -> List[Dict[str, Any]]:
    """Tema bazlı SKU + satış performansı + iade oranı."""
    conds = []
    params: Dict[str, Any] = {}
    if marka:
        conds.append("p.marka_adi = :marka"); params["marka"] = marka
    if sezon:
        conds.append("p.sezon_kodu = :sezon"); params["sezon"] = sezon
    where_p = ("WHERE " + " AND ".join(conds)) if conds else ""

    try:
        rows = (await session.execute(text(f"""
            WITH plm AS (
                SELECT tema_adi,
                       COUNT(*) AS sku,
                       COUNT(DISTINCT urun_grubu_adi) AS kategori,
                       ROUND((100.0*COUNT(*)/SUM(COUNT(*))OVER())::numeric, 1) AS pay
                FROM pim_products p {where_p}
                GROUP BY tema_adi
            ),
            sat AS (
                SELECT p.tema_adi,
                       ROUND(SUM(s.tutar)::numeric) AS brut_ciro,
                       ROUND(ABS(SUM(COALESCE(d.tutar, 0)))::numeric) AS iade_ciro
                FROM incorta_satis s
                JOIN pim_products p ON p.urun_kodu = s.urun_kodu
                LEFT JOIN incorta_depo_iade d
                       ON d.urun_kodu = s.urun_kodu AND d.ay = s.ay AND d.yil = s.yil
                {where_p.replace('p.', 'p.')}
                GROUP BY p.tema_adi
            )
            SELECT plm.tema_adi, plm.sku, plm.kategori, plm.pay,
                   COALESCE(sat.brut_ciro, 0) AS brut_ciro,
                   COALESCE(sat.iade_ciro, 0) AS iade_ciro,
                   ROUND((COALESCE(sat.iade_ciro, 0) /
                          NULLIF(sat.brut_ciro, 0) * 100)::numeric, 1) AS iade_pct
            FROM plm LEFT JOIN sat USING(tema_adi)
            ORDER BY plm.sku DESC LIMIT 15
        """), params)).mappings().all()

        return [
            {
                "tema": r["tema_adi"],
                "sku_sayisi": int(r["sku"]),
                "kategori_sayisi": int(r["kategori"]),
                "pay_pct": float(r["pay"] or 0),
                "brut_ciro": int(r["brut_ciro"]),
                "iade_ciro": int(r["iade_ciro"]),
                "iade_pct": float(r["iade_pct"] or 0),
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("urun_yonetimi.tema_error", error=str(e))
        return []


async def _fetch_kategori_analiz(
    session: AsyncSession,
    marka: Optional[str],
    sezon: Optional[str],
    tema: Optional[str],
) -> List[Dict[str, Any]]:
    """Ana grup + ürün grubu bazlı SKU ve satış."""
    conds = []
    params: Dict[str, Any] = {}
    if marka:
        conds.append("p.marka_adi = :marka"); params["marka"] = marka
    if sezon:
        conds.append("p.sezon_kodu = :sezon"); params["sezon"] = sezon
    if tema:
        conds.append("p.tema_adi = :tema"); params["tema"] = tema
    where_p = ("WHERE " + " AND ".join(conds)) if conds else ""

    try:
        rows = (await session.execute(text(f"""
            WITH plm AS (
                SELECT COALESCE(ana_grup_adi, 'Diğer') AS ana_grup,
                       COALESCE(urun_grubu_adi, 'Diğer') AS kategori,
                       COUNT(*) AS sku,
                       SUM(CASE WHEN internet_aktif THEN 1 ELSE 0 END) AS internet_aktif,
                       ROUND((100.0*COUNT(*)/SUM(COUNT(*))OVER())::numeric, 1) AS pay
                FROM pim_products p {where_p}
                GROUP BY COALESCE(ana_grup_adi,'Diğer'), COALESCE(urun_grubu_adi,'Diğer')
            ),
            sat AS (
                SELECT COALESCE(p.urun_grubu_adi, 'Diğer') AS kategori,
                       ROUND(SUM(s.tutar)::numeric) AS brut_ciro
                FROM incorta_satis s
                JOIN pim_products p ON p.urun_kodu = s.urun_kodu
                {where_p.replace('p.', 'p.')}
                GROUP BY COALESCE(p.urun_grubu_adi, 'Diğer')
            )
            SELECT plm.ana_grup, plm.kategori, plm.sku, plm.internet_aktif,
                   plm.pay, COALESCE(sat.brut_ciro, 0) AS brut_ciro
            FROM plm LEFT JOIN sat USING(kategori)
            ORDER BY plm.sku DESC LIMIT 20
        """), params)).mappings().all()

        return [
            {
                "ana_grup": r["ana_grup"],
                "kategori": r["kategori"],
                "sku_sayisi": int(r["sku"]),
                "internet_aktif": int(r["internet_aktif"]),
                "pay_pct": float(r["pay"] or 0),
                "brut_ciro": int(r["brut_ciro"]),
            }
            for r in rows
        ]
    except Exception as e:
        log.warning("urun_yonetimi.kategori_error", error=str(e))
        return []


async def _fetch_top_performans(
    session: AsyncSession,
    marka: Optional[str],
    sezon: Optional[str],
) -> Dict[str, Any]:
    """En çok satan + en yüksek iade oranlı PLM ürünleri."""
    conds = ["s.yil IN (2025, 2026)"]
    params: Dict[str, Any] = {}
    if marka:
        conds.append("p.marka_adi = :marka"); params["marka"] = marka
    if sezon:
        conds.append("p.sezon_kodu = :sezon"); params["sezon"] = sezon
    where = "WHERE " + " AND ".join(conds)

    try:
        top_rows = (await session.execute(text(f"""
            WITH sat AS (
                SELECT s.urun_kodu, MAX(s.urun_adi) urun_adi,
                       SUM(s.tutar) brut, SUM(s.adet::int) adet
                FROM incorta_satis s
                JOIN pim_products p ON p.urun_kodu = s.urun_kodu
                {where}
                GROUP BY s.urun_kodu
            ),
            iad AS (
                SELECT urun_kodu, ABS(SUM(tutar)) iade
                FROM incorta_depo_iade WHERE yil IN (2025, 2026)
                GROUP BY urun_kodu
            )
            SELECT sat.urun_kodu, sat.urun_adi,
                   ROUND(sat.brut::numeric) AS brut_ciro,
                   ROUND((sat.brut - COALESCE(iad.iade,0))::numeric) AS net_ciro,
                   ROUND((COALESCE(iad.iade,0)/NULLIF(sat.brut,0)*100)::numeric,1) AS iade_pct
            FROM sat LEFT JOIN iad USING(urun_kodu)
            ORDER BY net_ciro DESC LIMIT 5
        """), params)).mappings().all()

        risk_rows = (await session.execute(text(f"""
            WITH sat AS (
                SELECT s.urun_kodu, MAX(s.urun_adi) urun_adi, SUM(s.tutar) brut
                FROM incorta_satis s
                JOIN pim_products p ON p.urun_kodu = s.urun_kodu
                {where}
                GROUP BY s.urun_kodu HAVING SUM(s.tutar) > 30000
            ),
            iad AS (
                SELECT urun_kodu, ABS(SUM(tutar)) iade
                FROM incorta_depo_iade WHERE yil IN (2025, 2026)
                GROUP BY urun_kodu
            )
            SELECT sat.urun_kodu, sat.urun_adi,
                   ROUND(sat.brut::numeric) AS brut_ciro,
                   ROUND((COALESCE(iad.iade,0)/NULLIF(sat.brut,0)*100)::numeric,1) AS iade_pct
            FROM sat LEFT JOIN iad USING(urun_kodu)
            WHERE COALESCE(iad.iade,0)/NULLIF(sat.brut,0)*100 > 25
            ORDER BY iade_pct DESC LIMIT 5
        """), params)).mappings().all()

        return {
            "top_5_net_ciro": [
                {"urun_kodu": r["urun_kodu"], "urun_adi": r["urun_adi"],
                 "brut_ciro": int(r["brut_ciro"]), "net_ciro": int(r["net_ciro"]),
                 "iade_pct": float(r["iade_pct"] or 0)}
                for r in top_rows
            ],
            "yuksek_iade_riskli": [
                {"urun_kodu": r["urun_kodu"], "urun_adi": r["urun_adi"],
                 "brut_ciro": int(r["brut_ciro"]), "iade_pct": float(r["iade_pct"] or 0)}
                for r in risk_rows
            ],
        }
    except Exception as e:
        log.warning("urun_yonetimi.performans_error", error=str(e))
        return {}


# ── Ana fonksiyon ─────────────────────────────────────────────────────────────

async def run_urun_yonetimi_agent(
    session: AsyncSession,
    question: str,
    filters: Dict[str, Any],
    history: List[Dict[str, Any]],
    ton: str = "yonetici",
) -> Dict[str, Any]:
    t0 = time.perf_counter()

    marka = filters.get("marka") or None
    sezon = filters.get("sezon") or None
    tema  = filters.get("tema")  or None

    genel     = await _fetch_genel_ozet(session, marka, sezon, tema)
    sezon_lst = await _fetch_sezon_analiz(session, marka, tema)
    tema_lst  = await _fetch_tema_analiz(session, marka, sezon)
    kat_lst   = await _fetch_kategori_analiz(session, marka, sezon, tema)
    perf      = await _fetch_top_performans(session, marka, sezon)
    stok      = await _fetch_stok_live(question)

    ctx: Dict[str, Any] = {
        "filtre":          {"marka": marka, "sezon": sezon, "tema": tema},
        "genel_ozet":      genel,
        "sezon_analizi":   sezon_lst,
        "tema_analizi":    tema_lst,
        "kategori_analizi": kat_lst,
        "satis_performansi": perf,
    }
    if stok:
        ctx["stok_bilgisi"] = stok

    filtreler_str = json.dumps(ctx["filtre"], ensure_ascii=False)
    veri_str      = json.dumps(ctx, ensure_ascii=False, indent=2)
    ton_eki       = TON_EKLI.get(ton, TON_EKLI["yonetici"])

    system = get_date_context() + "\n\n" + URUN_YONETIMI_SYSTEM.format(
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

    # Kapsam dışı sinyal tespiti
    import re
    a2a = None
    if "[A2A_GEREKLİ:" in answer:
        m = re.search(r'\[A2A_GEREKLİ:\s*(\w+)[,\s]+([^\]]+)\]', answer)
        if m:
            a2a = {"hedef_agent": m.group(1), "soru": m.group(2).strip()}

    elapsed = round((time.perf_counter() - t0) * 1000, 1)
    log.info("urun_yonetimi_agent.done", elapsed_ms=elapsed,
             toplam_sku=genel.get("toplam_sku", 0))

    return {
        "answer":    answer,
        "elapsed_ms": elapsed,
        "agent":     "URUN_YONETIMI_AGENT",
        "a2a_signal": a2a,
        "veri_ozeti": {"toplam_sku": genel.get("toplam_sku", 0)},
    }
